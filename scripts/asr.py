#!/usr/bin/env python3
"""Transcribe a video. Local-first via ~/bin/listen, cloud Whisper as fallback.

Backends:
  - "local"   → shells out to ~/bin/listen, which routes to a local Qwen3-ASR
                endpoint (e.g. a Spark on the fleet) and falls back to the
                ScrappyLabs API. Free in the local case; flat-rate in the API
                case. No secret needed in this process.
  - "groq"    → Groq Whisper-large-v3 (cheaper, faster of the two clouds).
  - "openai"  → OpenAI whisper-1.

Resolution when backend=None: prefer local if the `listen` binary exists,
otherwise prefer Groq, otherwise OpenAI. The script never silently uploads
audio to a paid API when a local route is available.

Returns (segments, backend_used) where each segment is {start, end, text}.
Extracts audio identically across backends so the rest of the pipeline doesn't
care which path produced the transcript.
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"

OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = "whisper-1"

LISTEN_BIN_CANDIDATES = (
    Path.home() / "bin" / "listen",
    Path("/usr/local/bin/listen"),
    Path("/opt/homebrew/bin/listen"),
)


def _find_listen() -> Path | None:
    """Locate the `listen` wrapper. PATH lookup first, then well-known fallbacks."""
    found = shutil.which("listen")
    if found:
        return Path(found)
    for candidate in LISTEN_BIN_CANDIDATES:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _from_env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value else None


def _from_dotenv(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() != name:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            return value or None
    except OSError:
        return None
    return None


def _read_cloud_key(name: str) -> str | None:
    """Read a cloud API key from env or ~/.config/watch/.env or ./.env."""
    value = _from_env(name)
    if value:
        return value
    for candidate in (Path.home() / ".config" / "watch" / ".env", Path.cwd() / ".env"):
        value = _from_dotenv(candidate, name)
        if value:
            return value
    return None


def resolve_backend(preferred: str | None = None) -> tuple[str | None, str | None]:
    """Return (backend, api_key_or_None).

    `preferred` may be 'local', 'groq', 'openai', 'cloud' (= groq with openai
    fallback), or None (= auto: local first, then cloud).

    For 'local', the second tuple element is None — listen handles its own
    routing and auth.
    """
    if preferred == "local":
        if _find_listen() is None:
            return None, None
        return "local", None

    if preferred == "groq":
        key = _read_cloud_key("GROQ_API_KEY")
        return ("groq", key) if key else (None, None)

    if preferred == "openai":
        key = _read_cloud_key("OPENAI_API_KEY")
        return ("openai", key) if key else (None, None)

    if preferred == "cloud":
        key = _read_cloud_key("GROQ_API_KEY")
        if key:
            return "groq", key
        key = _read_cloud_key("OPENAI_API_KEY")
        return ("openai", key) if key else (None, None)

    # Auto: local → groq → openai.
    if _find_listen() is not None:
        return "local", None
    key = _read_cloud_key("GROQ_API_KEY")
    if key:
        return "groq", key
    key = _read_cloud_key("OPENAI_API_KEY")
    if key:
        return "openai", key
    return None, None


# Kept under the old name for backwards compat with anyone wrapping the skill.
load_api_key = resolve_backend


def extract_audio(video_path: str, out_path: Path) -> Path:
    """Extract mono 16kHz 64kbps mp3 — ~480 kB/min, fits any ASR upload limit."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "64k",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg audio extraction failed: {result.stderr.strip()}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SystemExit("ffmpeg produced no audio — video may have no audio track")
    return out_path


def _segments_from_response(data: dict) -> list[dict]:
    """Convert verbose_json (Whisper-shape) into our {start,end,text} segments.

    Falls back to a single zero-timestamp segment if the server returned only
    flat text (some local ASR servers don't honor response_format=verbose_json).
    """
    out: list[dict] = []
    for seg in data.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "start": round(float(seg.get("start") or 0.0), 2),
            "end": round(float(seg.get("end") or 0.0), 2),
            "text": text,
        })

    if not out:
        full = (data.get("text") or "").strip()
        if full:
            out.append({"start": 0.0, "end": 0.0, "text": full})

    return out


# ---------------- local backend (listen) ----------------

def _transcribe_local(audio_path: Path) -> list[dict]:
    listen_bin = _find_listen()
    if listen_bin is None:
        raise SystemExit(
            "`listen` not found on PATH or in ~/bin. Install the fleet listen wrapper, "
            "or pass --backend cloud / --backend groq / --backend openai."
        )

    print(f"[watch] transcribing via {listen_bin} (local-first)…", file=sys.stderr)

    try:
        result = subprocess.run(
            [str(listen_bin), "--segments", str(audio_path)],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("listen timed out after 600s")
    except FileNotFoundError as exc:
        raise SystemExit(f"listen invocation failed: {exc}")

    if result.returncode != 0:
        # Surface stderr and let the caller decide whether to fall back.
        raise SystemExit(f"listen exited {result.returncode}: {result.stderr.strip()}")

    payload = result.stdout.strip()
    if not payload:
        raise SystemExit("listen returned no output")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # Older listen wrappers (or text mode) may return raw text. Treat as
        # one untimed segment so the report still has *something*.
        return [{"start": 0.0, "end": 0.0, "text": payload}]

    return _segments_from_response(data)


# ---------------- cloud backend (Whisper) ----------------

def _build_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----WatchBoundary{uuid.uuid4().hex}"
    eol = b"\r\n"
    buf = io.BytesIO()

    for name, value in fields.items():
        buf.write(f"--{boundary}".encode()); buf.write(eol)
        buf.write(f'Content-Disposition: form-data; name="{name}"'.encode()); buf.write(eol)
        buf.write(eol)
        buf.write(str(value).encode()); buf.write(eol)

    mimetype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    buf.write(f"--{boundary}".encode()); buf.write(eol)
    buf.write(
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"'.encode()
    )
    buf.write(eol)
    buf.write(f"Content-Type: {mimetype}".encode()); buf.write(eol)
    buf.write(eol)
    buf.write(file_path.read_bytes())
    buf.write(eol)
    buf.write(f"--{boundary}--".encode()); buf.write(eol)

    return buf.getvalue(), boundary


MAX_ATTEMPTS = 4
MAX_429_RETRIES = 2
RETRY_BASE_DELAY = 2.0


def _post_whisper(endpoint: str, api_key: str, model: str, audio_path: Path) -> dict:
    fields = {
        "model": model,
        "response_format": "verbose_json",
        "temperature": "0",
    }
    body, boundary = _build_multipart(fields, audio_path)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        # Groq sits behind Cloudflare; default Python-urllib UA trips WAF before auth.
        "User-Agent": "watch-skill/2.0 (+scrappylabs; python-urllib)",
    }

    context = ssl.create_default_context()
    rate_limit_hits = 0
    last_exc: Exception | None = None
    last_detail = ""

    for attempt in range(MAX_ATTEMPTS):
        request = Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=300, context=context) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            last_exc, last_detail = exc, detail

            if 400 <= exc.code < 500 and exc.code != 429:
                raise SystemExit(f"Whisper request failed: {exc}{detail}")

            if exc.code == 429:
                rate_limit_hits += 1
                if rate_limit_hits >= MAX_429_RETRIES:
                    raise SystemExit(f"Whisper request failed: {exc}{detail}")
                delay = _retry_after(exc) or RETRY_BASE_DELAY * (2 ** attempt) + 1
            else:
                delay = RETRY_BASE_DELAY * (2 ** attempt)

            if attempt < MAX_ATTEMPTS - 1:
                print(
                    f"[watch] whisper HTTP {exc.code} — retrying in {delay:.1f}s "
                    f"(attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, OSError) as exc:
            last_exc, last_detail = exc, ""
            if attempt < MAX_ATTEMPTS - 1:
                delay = RETRY_BASE_DELAY * (attempt + 1)
                print(
                    f"[watch] whisper network error ({type(exc).__name__}: {exc}) — "
                    f"retrying in {delay:.1f}s (attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
            continue

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Whisper returned non-JSON response: {exc}: {payload[:200]}")

    raise SystemExit(
        f"Whisper request failed after {MAX_ATTEMPTS} attempts: {last_exc}{last_detail}"
    )


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except Exception:
        return ""
    if not body:
        return ""
    try:
        return f" — {body.decode('utf-8', errors='replace')[:400]}"
    except Exception:
        return ""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    header = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


# ---------------- public entry ----------------

def transcribe_video(
    video_path: str,
    audio_out: Path,
    backend: str | None = None,
    api_key: str | None = None,
) -> tuple[list[dict], str]:
    """Run extract → transcribe pipeline. Returns (segments, backend_used)."""
    if backend is None or (backend != "local" and api_key is None):
        detected_backend, detected_key = resolve_backend(backend)
        backend = backend or detected_backend
        if backend != "local":
            api_key = api_key or detected_key

    if not backend:
        raise SystemExit(
            "No transcription backend available. Install `~/bin/listen` for the "
            "local fleet ASR, or set GROQ_API_KEY / OPENAI_API_KEY in the "
            "environment or ~/.config/watch/.env."
        )

    if backend != "local" and not api_key:
        raise SystemExit(
            f"No API key for backend={backend}. "
            f"Set {('GROQ_API_KEY' if backend == 'groq' else 'OPENAI_API_KEY')}."
        )

    print(f"[watch] extracting audio for ASR ({backend})…", file=sys.stderr)
    audio_path = extract_audio(video_path, audio_out)
    size_kb = audio_path.stat().st_size / 1024

    if backend == "local":
        segments = _transcribe_local(audio_path)
    elif backend == "groq":
        print(f"[watch] audio: {size_kb:.0f} kB — uploading to Groq Whisper…", file=sys.stderr)
        response = _post_whisper(GROQ_ENDPOINT, api_key, GROQ_MODEL, audio_path)
        segments = _segments_from_response(response)
    elif backend == "openai":
        print(f"[watch] audio: {size_kb:.0f} kB — uploading to OpenAI Whisper…", file=sys.stderr)
        response = _post_whisper(OPENAI_ENDPOINT, api_key, OPENAI_MODEL, audio_path)
        segments = _segments_from_response(response)
    else:
        raise SystemExit(f"Unknown ASR backend: {backend}")

    if not segments:
        raise SystemExit(f"{backend} returned no transcript segments")

    print(f"[watch] transcribed {len(segments)} segments via {backend}", file=sys.stderr)
    return segments, backend


# Backwards-compat alias for any external caller still importing the old name.
transcribe = transcribe_video


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "usage: asr.py <video-path> [<audio-out.mp3>] [--backend local|cloud|groq|openai]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    video = sys.argv[1]
    audio_out = Path(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else Path("audio.mp3")
    backend_override = None
    if "--backend" in sys.argv:
        backend_override = sys.argv[sys.argv.index("--backend") + 1]

    segments, backend = transcribe_video(video, audio_out, backend=backend_override)
    print(json.dumps({"backend": backend, "segments": segments}, indent=2))
