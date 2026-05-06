# Changelog

All notable changes to `/watch` are documented here.

## [1.2.0] — 2026-05-05

### Added
- **Sibling-subtitle pickup for local files**: `resolve_local()` now searches the video's parent directory for matching `.vtt`/`.srt` files (e.g. yt-dlp's `video.en.vtt` next to `video.mp4`), preferring English variants. Re-running `watch` on a previously-downloaded file now picks up the cached captions instead of falling through to ASR.
- **Audio chunking when over backend upload limit**: `transcribe_video()` now splits oversized audio into time-based chunks (10 min for local listen, 25 min for cloud Whisper), transcribes each, and stitches segments back together with per-chunk timestamp offsets. Local `~/bin/listen` returns HTTP 400 on files >10 MB; this fixes the failure mode for any video over ~21 minutes routed through local ASR. Cloud limit is 25 MB (~52 min); chunking covers anything longer.

## [1.1.0] — 2026-05-05

### Added
- **`--no-frames` flag** on `watch.py`: skip frame extraction entirely and return a transcript-only report. Saves ~50-80k image tokens on talking-head content (interviews, podcasts, fireside chats) where the visuals are just two people on Zoom and add no signal. SKILL.md now nudges the caller to default to `--no-frames` for podcast/interview titles and re-run with frames only if the transcript references something visual.

## [1.0.0] — 2026-05-05

### ScrappyLabs fork

Forked from [`bradautomates/claude-video`](https://github.com/bradautomates/claude-video) at v0.1.2.

### Added
- **Local-first ASR backend**: routes audio transcription through `~/bin/listen` when present. The wrapper hits a local Qwen3-ASR endpoint first (free, fleet-internal) and falls back to `api.scrappylabs.ai` automatically — no per-call cost, no key in this process.
- New `--backend {local,cloud,groq,openai}` flag on `watch.py`. `local` is selected automatically when `~/bin/listen` is on PATH.
- New `--segments` flag on `~/bin/listen` (added separately) so the local route can request `verbose_json` and return timestamped segments compatible with the Whisper-shape parser.
- `~/bin/watch` symmetric wrapper, matching `~/bin/speak` and `~/bin/listen`. Looks up the skill in `$WATCH_SKILL_DIR`, plugin cache, `~/.claude/skills/watch/`, and `~/.codex/skills/watch/`.

### Changed
- `scripts/whisper.py` → `scripts/asr.py`. Same public surface (`transcribe_video`, returning `(segments, backend)`) but with an extra `local` backend and a new `resolve_backend()` helper that prefers local over cloud.
- `scripts/setup.py` no longer treats a missing API key as an error when `~/bin/listen` is installed — local ASR is a first-class backend.
- SessionStart hook detects `~/bin/listen` and stays silent if either local ASR or a cloud key is configured.
- README/SKILL.md rewritten to document the local-first path and the cloud opt-in.
- Plugin manifests rebranded to ScrappyLabs.

### Kept
- All upstream frame-budget logic, yt-dlp caption parsing, focused-mode behavior, and security model. The cloud Whisper path (Groq + OpenAI) is unchanged when explicitly selected via `--backend groq` / `--backend openai` / `--backend cloud`, or when `~/bin/listen` is unavailable.

### Compatibility
- Old `--whisper {groq,openai}` and `--no-whisper` flags are kept as aliases.
- Old `whisper.py` import path is removed; downstream scripts should `from asr import …` instead.

---

## Upstream history (bradautomates/claude-video)

## [0.1.2] — 2026-04-24

### Fixed
- Windows console crash: removed the emoji from the long-video warning in `watch.py`; cp1252 consoles couldn't encode it.
- `setup.py` now prints `winget` / `pip` install commands on Windows instead of "unsupported platform" — matches what the README already promised.

### Changed
- `SKILL.md` notes that on Windows the scripts must be invoked with `python`, not `python3` (the latter is the Microsoft Store stub on Windows).

## [0.1.1] — 2026-04-24

### Fixed
- Added `commands/watch.md` shim so `/watch` is callable when installed as a Claude Code plugin. Without it, the plugin loaded but the skill wasn't exposed as a slash command.
- `scripts/build-skill.sh` now strips `commands/` from the claude.ai `.skill` bundle alongside `hooks/` and `.claude-plugin/`.

## [0.1.0] — 2026-04-24

Initial marketplace release.

### Added
- `/watch <url-or-path> [question]` slash command.
- yt-dlp download with native caption extraction (manual + auto-subs).
- ffmpeg frame extraction with auto-scaled fps (≤2 fps, ≤100 frames, duration-aware budget).
- `--start` / `--end` focused mode with denser frame budget and transcript range filtering.
- Whisper fallback (Groq preferred, OpenAI secondary) for videos without captions.
- `setup.py` preflight: silent `--check`, structured `--json`, and installer that auto-runs `brew install` on macOS.
- Session-start hook that prints a one-line status on first run / partial config.
- `.skill` bundle packaging for claude.ai upload via `scripts/build-skill.sh`.
