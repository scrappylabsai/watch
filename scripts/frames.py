#!/usr/bin/env python3
"""Probe video metadata and select frames for Claude to read.

Default strategy is scene-aware: one ffmpeg pass grabs every scene change plus
a density floor (at least one frame every N seconds), then a sliding-window
pixel dedup drops shots the model has already seen, then the survivors are
thinned evenly to the duration-scaled budget. A static screencast collapses to
a handful of frames while a fast-cut reel keeps every beat — far fewer wasted
image tokens than fixed-interval sampling.

Fixed-interval extraction is kept as the fallback (and is forced by --fps):
auto-fps targets a frame budget, not a fixed rate, so short videos stay dense
and long videos stay capped. When a user-specified range is passed,
focused-mode budgets denser (they are zooming in for detail).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


MAX_FPS = 2.0

# Scene-aware selection defaults (tunable via flags).
SCENE_DEFAULT = 0.30            # ffmpeg scene-change score a cut must exceed
DEDUP_THRESHOLD_DEFAULT = 8.0   # % of thumbnail bytes that must change to count as new
DEDUP_WINDOW_DEFAULT = 4        # compare against the last N kept frames (A-B-A cutaways)
DEDUP_PIXEL_DELTA = 30          # per-byte 0-255 delta that counts as a changed pixel
THUMB_SIDE = 48                 # dedup thumbnails are THUMB_SIDE x THUMB_SIDE rgb24
THUMB_BYTES = THUMB_SIDE * THUMB_SIDE * 3
FLOOR_MIN_SECONDS = 0.5         # floor never demands more than 2 fps
FLOOR_MAX_SECONDS = 10.0        # ...and never lets a gap exceed 10s

_SHOWINFO_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def _clamp_fps(fps: float, duration_seconds: float, max_frames: int) -> tuple[float, int]:
    fps = min(fps, MAX_FPS)
    target = min(max_frames, max(1, int(round(fps * duration_seconds))))
    return fps, target


def parse_time(value: str | float | int | None) -> float | None:
    """Parse SS, MM:SS, or HH:MM:SS (with optional .ms) into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SystemExit(f"Cannot parse time value: {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def format_time(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def get_metadata(video_path: str) -> dict:
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install with: brew install ffmpeg")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or video_stream.get("duration") or 0)
    return {
        "duration_seconds": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
        "size_bytes": int(fmt.get("size") or 0),
        "has_audio": audio_stream is not None,
    }


def auto_fps(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Pick fps that targets a sensible frame budget for full-video scans."""
    if duration_seconds <= 0:
        return 1.0, 1

    if duration_seconds <= 30:
        target = min(max_frames, max(12, int(round(duration_seconds))))
    elif duration_seconds <= 60:
        target = min(max_frames, 40)
    elif duration_seconds <= 180:  # 3 min
        target = min(max_frames, 60)
    elif duration_seconds <= 600:  # 10 min
        target = min(max_frames, 80)
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def auto_fps_focus(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Denser budget for user-specified ranges — they are zooming in for detail."""
    if duration_seconds <= 0:
        return min(MAX_FPS, 2.0), 2

    if duration_seconds <= 5:
        target = min(max_frames, max(10, int(round(duration_seconds * 6))))
    elif duration_seconds <= 15:
        target = min(max_frames, max(30, int(round(duration_seconds * 4))))
    elif duration_seconds <= 30:
        target = min(max_frames, 60)
    elif duration_seconds <= 60:
        target = min(max_frames, 80)
    elif duration_seconds <= 180:
        target = max_frames
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def scene_floor(duration_seconds: float, budget: int) -> float:
    """Density-floor spacing: alone it would yield ≈budget frames; scene cuts add more."""
    if duration_seconds <= 0 or budget <= 0:
        return 1.0
    return min(FLOOR_MAX_SECONDS, max(FLOOR_MIN_SECONDS, duration_seconds / budget))


def _differs(a: bytes, b: bytes, changed_limit: int) -> bool:
    """True once more than changed_limit bytes differ by > DEDUP_PIXEL_DELTA (early exit)."""
    changed = 0
    for x, y in zip(a, b):
        d = x - y if x >= y else y - x
        if d > DEDUP_PIXEL_DELTA:
            changed += 1
            if changed > changed_limit:
                return True
    return False


def _dedup_keep(thumbs: list[bytes], threshold_pct: float, window: int) -> list[int]:
    """Indices of thumbs that survive sliding-window dedup, in order."""
    changed_limit = int(THUMB_BYTES * max(0.0, threshold_pct) / 100.0)
    kept: list[int] = []
    for i, thumb in enumerate(thumbs):
        recent = kept[-window:] if window > 0 else []
        if any(not _differs(thumb, thumbs[j], changed_limit) for j in recent):
            continue
        kept.append(i)
    return kept


def _thin(indices: list[int], cap: int) -> list[int]:
    """Evenly thin a sorted index list down to cap, keeping first and last."""
    if len(indices) <= cap:
        return indices
    if cap <= 1:
        return indices[:1]
    last = len(indices) - 1
    picked: list[int] = []
    for k in range(cap):
        candidate = indices[round(k * last / (cap - 1))]
        if not picked or candidate != picked[-1]:
            picked.append(candidate)
    return picked


def extract_scene(
    video_path: str,
    out_dir: Path,
    *,
    budget: int,
    scene: float = SCENE_DEFAULT,
    floor: float | None = None,
    resolution: int = 512,
    dedup_threshold: float = DEDUP_THRESHOLD_DEFAULT,
    dedup_window: int = DEDUP_WINDOW_DEFAULT,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> dict | None:
    """Scene-aware frame selection. Returns {frames, candidates, kept, scene, floor},
    or None when the caller should fall back to fixed-interval extract()."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()
    for existing in out_dir.glob("cand_*.jpg"):
        existing.unlink()
    thumbs_raw = out_dir / "thumbs.raw"
    thumbs_raw.unlink(missing_ok=True)

    if floor is None:
        floor = 1.0
    # Candidate cap only exists to bound a pathological strobe cut; hitting it
    # means selection got truncated mid-video, so we bail to even sampling.
    cand_cap = min(600, max(200, budget * 4))

    select_expr = (
        f"isnan(prev_selected_t)+gt(scene,{scene:.4f})+gte(t-prev_selected_t,{floor:.4f})"
    )
    graph = (
        f"[0:v]select='{select_expr}',showinfo,split=2[full][small];"
        f"[full]scale={resolution}:-2[fullout];"
        f"[small]scale={THUMB_SIDE}:{THUMB_SIDE},format=rgb24[thumbout]"
    )

    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostats", "-y"]
    # -ss before -i = fast seek (keyframe-snap), matching extract().
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]
    cmd += [
        "-i", video_path,
        "-filter_complex", graph,
        "-map", "[fullout]", "-fps_mode", "vfr", "-frames:v", str(cand_cap), "-q:v", "4",
        str(out_dir / "cand_%04d.jpg"),
        "-map", "[thumbout]", "-fps_mode", "vfr", "-frames:v", str(cand_cap),
        "-f", "rawvideo", str(thumbs_raw),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    candidates = sorted(out_dir.glob("cand_*.jpg"))

    def _bail() -> None:
        for p in candidates:
            p.unlink(missing_ok=True)
        thumbs_raw.unlink(missing_ok=True)

    if result.returncode != 0 or not candidates or len(candidates) >= cand_cap:
        _bail()
        return None

    offset = start_seconds or 0.0
    times = [float(t) for t in _SHOWINFO_PTS_RE.findall(result.stderr)]
    if len(times) != len(candidates):
        # showinfo parse hiccup — approximate timestamps by even spacing so the
        # report stays usable rather than dropping the whole selection.
        end = end_seconds if end_seconds is not None else 0.0
        span = max(0.0, end - offset) if end_seconds is not None else 0.0
        if span <= 0:
            span = floor * len(candidates)
        step = span / max(1, len(candidates))
        times = [i * step for i in range(len(candidates))]

    thumb_data = thumbs_raw.read_bytes() if thumbs_raw.exists() else b""
    if len(thumb_data) == THUMB_BYTES * len(candidates):
        thumbs = [
            thumb_data[i * THUMB_BYTES:(i + 1) * THUMB_BYTES]
            for i in range(len(candidates))
        ]
        kept = _dedup_keep(thumbs, dedup_threshold, dedup_window)
    else:
        kept = list(range(len(candidates)))  # thumb stream mismatch: skip dedup

    kept = _thin(kept, max(1, budget))

    frames: list[dict] = []
    for new_idx, ci in enumerate(kept):
        dst = out_dir / f"frame_{new_idx + 1:04d}.jpg"
        candidates[ci].rename(dst)
        frames.append({
            "index": new_idx,
            "timestamp_seconds": round(offset + times[ci], 2),
            "path": str(dst),
        })

    for p in out_dir.glob("cand_*.jpg"):
        p.unlink()
    thumbs_raw.unlink(missing_ok=True)

    return {
        "frames": frames,
        "candidates": len(candidates),
        "kept": len(frames),
        "scene": scene,
        "floor": floor,
    }


def extract(
    video_path: str,
    out_dir: Path,
    fps: float,
    resolution: int = 512,
    max_frames: int = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> list[dict]:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]

    # -ss before -i = fast seek (keyframe-snap, good enough for preview frames).
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]

    cmd += [
        "-i", video_path,
        "-vf", f"fps={fps},scale={resolution}:-2",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        output_pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg frame extraction failed: {result.stderr.strip()}")

    offset = start_seconds or 0.0
    frames = sorted(out_dir.glob("frame_*.jpg"))
    return [
        {
            "index": i,
            "timestamp_seconds": round(offset + (i / fps if fps > 0 else 0.0), 2),
            "path": str(p),
        }
        for i, p in enumerate(frames)
    ]


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "usage: frames.py <video-path> <out-dir> [--fps F] [--fixed-interval] "
            "[--scene S] [--dedup-threshold P] [--dedup-window N] "
            "[--resolution W] [--max-frames N] [--start T] [--end T]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    video = sys.argv[1]
    out = Path(sys.argv[2])
    args = sys.argv[3:]

    fps_override = None
    resolution = 512
    max_frames = 100
    start_arg = None
    end_arg = None
    fixed_interval = False
    scene_threshold = SCENE_DEFAULT
    dedup_threshold = DEDUP_THRESHOLD_DEFAULT
    dedup_window = DEDUP_WINDOW_DEFAULT
    i = 0
    while i < len(args):
        if args[i] == "--fps":
            fps_override = float(args[i + 1]); i += 2
        elif args[i] == "--fixed-interval":
            fixed_interval = True; i += 1
        elif args[i] == "--scene":
            scene_threshold = float(args[i + 1]); i += 2
        elif args[i] == "--dedup-threshold":
            dedup_threshold = float(args[i + 1]); i += 2
        elif args[i] == "--dedup-window":
            dedup_window = int(args[i + 1]); i += 2
        elif args[i] == "--resolution":
            resolution = int(args[i + 1]); i += 2
        elif args[i] == "--max-frames":
            max_frames = int(args[i + 1]); i += 2
        elif args[i] == "--start":
            start_arg = args[i + 1]; i += 2
        elif args[i] == "--end":
            end_arg = args[i + 1]; i += 2
        else:
            i += 1

    meta = get_metadata(video)
    start_sec = parse_time(start_arg)
    end_sec = parse_time(end_arg)
    full_duration = meta["duration_seconds"]

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)

    focused = start_sec is not None or end_sec is not None
    if focused:
        fps, target = auto_fps_focus(effective_duration, max_frames=max_frames)
    else:
        fps, target = auto_fps(effective_duration, max_frames=max_frames)
    if fps_override is not None:
        fps = fps_override
        target = max(1, int(round(fps * effective_duration)))

    selection = None
    if fps_override is None and not fixed_interval:
        selection = extract_scene(
            video, out,
            budget=target,
            scene=scene_threshold,
            floor=scene_floor(effective_duration, target),
            resolution=resolution,
            dedup_threshold=dedup_threshold,
            dedup_window=dedup_window,
            start_seconds=start_sec,
            end_seconds=end_sec,
        )

    if selection is not None:
        frames = selection["frames"]
    else:
        frames = extract(
            video, out,
            fps=fps,
            resolution=resolution,
            max_frames=max_frames,
            start_seconds=start_sec,
            end_seconds=end_sec,
        )
    print(json.dumps(
        {
            "meta": meta,
            "fps": fps,
            "target": target,
            "focused": focused,
            "selection": (
                {k: selection[k] for k in ("candidates", "kept", "scene", "floor")}
                if selection else {"mode": "fixed-interval"}
            ),
            "frames": frames,
        },
        indent=2,
    ))
