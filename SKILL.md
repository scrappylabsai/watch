---
name: watch
description: Watch a video (URL or local path). Downloads with yt-dlp, extracts scene-aware deduplicated frames with ffmpeg (every real cut + a density floor, repeated shots sent once), pulls the transcript from captions (or local-first ASR via ~/bin/listen, with cloud Whisper fallback), and hands the result to Claude so it can answer questions about what's in the video.
argument-hint: "<video-url-or-path> [question]"
allowed-tools: Bash, Read, AskUserQuestion
homepage: https://github.com/scrappylabsai/watch
repository: https://github.com/scrappylabsai/watch
author: scrappylabsai
license: MIT
user-invocable: true
---

# /watch — Claude watches a video

You don't have a video input; this skill gives you one. A Python script downloads the video, extracts scene-aware frames as JPEGs (every real scene change plus a density floor, with near-duplicate shots deduped so you never re-see a frame), gets a timestamped transcript (native captions first, then local-first ASR via `~/bin/listen`, then optional cloud Whisper as a final fallback), and prints frame paths. You then `Read` each frame path to see the images and combine them with the transcript to answer the user.

## Step 0 — Setup preflight (runs every `/watch` invocation, silent on success)

**Python interpreter:** every `python3 ...` command in this skill is for macOS/Linux. On **Windows**, substitute `python` — the `python3` command on Windows is the Microsoft Store stub and will not run the script.

Before every `/watch` run, verify that dependencies and an ASR backend are in place:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py" --check
```

This is a <100ms lookup. On exit 0, the script emits **nothing** — proceed to Step 1 without comment. **Do NOT announce "setup is complete" to the user** — they don't need a status message on every turn. The only acceptable user-visible output from Step 0 is when remediation is required.

On non-zero exit, follow the table:

| Exit | Meaning | Action |
|------|---------|--------|
| `2` | Missing binaries (`ffmpeg` / `ffprobe` / `yt-dlp`) | Run installer |
| `3` | No ASR backend (`~/bin/listen` not present AND no cloud key) | Run installer; ask user whether to install the local `listen` wrapper or use a cloud Whisper key |
| `4` | Both missing | Run installer, then ask about ASR backend |

The installer is idempotent — safe to re-run:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py"
```

On macOS with Homebrew, it auto-installs `ffmpeg` and `yt-dlp`. On Linux/Windows, it prints the exact install commands for the user to run. It scaffolds `~/.config/watch/.env` with commented placeholders at `0600` perms, and writes `SETUP_COMPLETE=true` once deps + an ASR backend are in place so the next session knows this user has already been through the wizard.

**If no ASR backend is configured:** use `AskUserQuestion` to ask whether the user wants the **local** path (install `~/bin/listen` from the ScrappyLabs fleet — free, prefers an OpenAI-compatible local Qwen3-ASR endpoint with `api.scrappylabs.ai` as automatic fallback) or the **cloud** path (Groq or OpenAI Whisper API key). For the cloud path, write the key into `~/.config/watch/.env` — set the matching `GROQ_API_KEY=...` or `OPENAI_API_KEY=...` line. If they don't want either, proceed with `--no-asr` and tell them videos without native captions will come back frames-only.

**Structured mode (optional):** `python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py" --json` emits `{status, first_run, missing_binaries, asr_backend, has_api_key, has_local_listen, config_file, platform}` where `status` is one of `ready | needs_install | needs_key | needs_install_and_key`. Use this when you need to branch on specifics (e.g. "does this user have local listen installed?" → `has_local_listen: true`).

Within a single session, you can skip Step 0 on follow-up `/watch` calls — once `--check` returned 0, nothing about the environment changes between turns.

## When to use

- User pastes a video URL (YouTube, Vimeo, X, TikTok, Twitch clip, most yt-dlp-supported sites) and asks about it.
- User points at a local video file (`.mp4`, `.mov`, `.mkv`, `.webm`, etc.) and asks about it.
- User types `/watch <url-or-path> [question]`.

## Frame selection (scene-aware by default)

Frames are selected by content, not by a fixed clock:

1. **Scene detection** — one ffmpeg pass keeps every frame whose scene-change score exceeds `--scene` (default 0.30), so fast cuts are caught even between whole-second marks.
2. **Density floor** — at least one frame every N seconds regardless of cuts (N auto-scales: duration ÷ budget, clamped 0.5-10s), so slow screencasts still get coverage.
3. **Dedup** — each candidate is pixel-diffed (downscaled RGB) against the last `--dedup-window` kept frames (default 4); candidates where fewer than `--dedup-threshold`% of pixels changed (default 8) are dropped. A static slide collapses to one frame; an A-B-A cutaway doesn't re-send shot A.
4. **Budget thinning** — survivors are evenly thinned to the duration-scaled budget below.

The practical effect: static/talking content comes in **well under** budget (a 27s screencast → ~10 frames instead of 27), while fast-cut reels keep every beat. If scene detection fails or the video is a pathological strobe, the script silently falls back to fixed-interval sampling.

## Recommended limits

- **Best accuracy: videos under 10 minutes.** Frame coverage scales inversely with duration.
- **Hard caps: 100 frames total and 2 fps.** Token cost grows with frame count, so the script caps frames with a budget by duration — scene-aware selection typically comes in under it:
  - ≤30s → budget ~12-30 frames
  - 30s-1min → ~40 frames
  - 1-3min → ~60 frames
  - 3-10min → ~80 frames
  - \>10min → 100 frames, sparsely spaced (warning printed)
- If the user hands you a long video, consider asking whether they want a specific section before burning tokens on a sparse scan.

## How to invoke

**Step 1 — parse the user input.** Separate the video source (URL or path) from any question the user asked. Example: `/watch https://youtu.be/abc what language is this in?` → source = `https://youtu.be/abc`, question = `what language is this in?`.

**Step 2 — run the watch script.** Pass the source verbatim. Do not shell-escape it yourself beyond normal quoting:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/watch.py" "<source>"
```

Optional flags:
- `--start T` / `--end T` — focus on a section. Accepts `SS`, `MM:SS`, or `HH:MM:SS`. When either is set, fps auto-scales denser (see "Focusing on a section" below).
- `--max-frames N` — lower the cap for tighter token budget (e.g. `--max-frames 40`)
- `--resolution W` — change frame width in px (default 512; bump to 1024 only if the user needs to read on-screen text)
- `--scene S` — scene-change sensitivity 0-1 (default 0.30; lower = more candidate frames). Drop to ~0.15 if subtle transitions are being missed.
- `--dedup-threshold P` — % of pixels that must change for a candidate to survive dedup (default 8; `0` disables dedup). Raise to ~15 for noisy/handheld footage that isn't deduping.
- `--dedup-window N` — compare each candidate against the last N kept frames (default 4)
- `--fixed-interval` — force legacy evenly-spaced sampling instead of scene-aware selection
- `--fps F` — override auto-fps (clamped to 2 fps max; implies `--fixed-interval`)
- `--out-dir DIR` — keep working files somewhere specific (default: an auto-generated tmp dir)
- `--backend local|cloud|groq|openai` — force a specific ASR backend. Default: `local` if `~/bin/listen` exists, else `cloud` (Groq with OpenAI fallback). `--whisper {groq,openai}` is kept as a backwards-compat alias.
- `--no-asr` — disable the ASR fallback entirely (frames-only if no captions). `--no-whisper` is kept as a backwards-compat alias.
- `--no-frames` — skip frame extraction; transcript-only output. **Use this for talking-head content** — interviews, podcasts, fireside chats, panel discussions, audio books with a static cover. Two heads on Zoom for 50 minutes burns ~50-80k image tokens to tell you "yep, two heads on Zoom." When the visuals add no signal, drop them. Heuristic: if the title/channel says "podcast", "interview", "fireside", "AMA", or "in conversation with", default to `--no-frames`. If you're unsure, the safer bet is to start with `--no-frames` and re-run with frames if the transcript references something visual ("look at this graph", "as you can see on screen").

### Focusing on a section (higher frame rate)

When the user asks about a specific moment — "what happens at the 2 minute mark?", "zoom into 0:45 to 1:00", "the first 10 seconds" — pass `--start` and/or `--end`. Scene-aware selection still applies inside the range, but with denser focused-mode budgets (and thus a tighter density floor; fixed-interval equivalents below, still capped at 2 fps):

- ≤5s → 2 fps (up to 10 frames)
- 5-15s → 2 fps (up to 30 frames)
- 15-30s → ~2 fps (up to 60 frames)
- 30-60s → ~1.3 fps (up to 80 frames)
- 60-180s → ~0.6 fps (100 frames, capped)

Focused mode is the right call for:
- Any moment/range the user names explicitly ("around 2:30", "the intro", "the last 30 seconds").
- Any video longer than ~10 minutes where the user's question is about a specific part — running focused on the relevant section is far more useful than a sparse scan of the whole thing.
- Re-runs after a full scan didn't have enough detail in some region.

Transcript is auto-filtered to the same range. Frame timestamps are absolute (real video timeline, not offset-from-start).

Examples:
```bash
# Last 10 seconds of a 1 minute video
python3 "${CLAUDE_SKILL_DIR}/scripts/watch.py" video.mp4 --start 50 --end 60

# Zoom into 2:15 → 2:45 at 3 fps (90 frames)
python3 "${CLAUDE_SKILL_DIR}/scripts/watch.py" "$URL" --start 2:15 --end 2:45 --fps 3

# From 1h12m to the end of the video
python3 "${CLAUDE_SKILL_DIR}/scripts/watch.py" "$URL" --start 1:12:00
```

**Step 3 — Read every frame path the script lists.** The Read tool renders JPEGs directly as images for you. Read all frames in a single message (parallel tool calls) so you see them together. The frames are in chronological order with a `t=MM:SS` timestamp so you can align them to the transcript.

**Step 4 — answer the user.** You now have two streams of evidence:
- **Frames** — what's on screen at each timestamp
- **Transcript** — what's said at each timestamp. The report's header shows the source: `captions` = yt-dlp pulled native subs; `listen (local)` = local-first ASR via `~/bin/listen`; `whisper (groq)` or `whisper (openai)` = transcribed by cloud API.

If the user asked a specific question, answer it directly citing timestamps. If they didn't ask anything, summarize what happens in the video — structure, key moments, notable visuals, spoken content.

**Step 5 — clean up.** The script prints a working directory at the end. If the user isn't going to ask follow-ups about this video, delete it with `rm -rf <dir>`. If they might, leave it in place.

## Transcription

The script gets a timestamped transcript in one of three ways:

1. **Native captions (free, preferred).** yt-dlp pulls manual or auto-generated subtitles from the source platform if available.
2. **Local-first ASR via `~/bin/listen` ($0 — all ScrappyLabs infra, default).** When `~/bin/listen` is on PATH, the script extracts audio (`ffmpeg -vn -ac 1 -ar 16000 -b:a 64k`, ~0.5 MB/min) and pipes it through `listen --segments`, which:
   - tries a local OpenAI-compatible ASR endpoint (Qwen3-ASR on a Spark) first,
   - falls back to `https://api.scrappylabs.ai/v1/audio/transcriptions` if the local one isn't reachable. The gateway round-robins to fleet Sparks, so either way transcription runs on hardware we own.
   No keys are exposed to this process — `listen` handles its own auth. Zero marginal cost.
3. **Cloud Whisper API (off-fleet fallback).** If `~/bin/listen` isn't installed (or the user explicitly passes `--backend cloud` / `--backend groq` / `--backend openai`), the same audio is uploaded to a Whisper API:
   - **Groq** — `whisper-large-v3`. Preferred cloud default: cheaper, faster. Get a key at console.groq.com/keys.
   - **OpenAI** — `whisper-1`. Cloud fallback. Get a key at platform.openai.com/api-keys.

Cloud keys live in `~/.config/watch/.env`. The script prefers Groq when both are set; override with `--backend openai` to force OpenAI. Use `--no-asr` to skip transcription entirely.

## Failure modes and handling

- **Setup preflight failed** → run `python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py"` (auto-installs ffmpeg/yt-dlp via brew on macOS, scaffolds the `.env`). For ASR backend, ask the user via `AskUserQuestion` whether they want the local `~/bin/listen` route or a cloud Whisper key, and configure accordingly.
- **No transcript available** → captions missing AND no ASR backend (no `~/bin/listen`, no cloud key, or the chosen backend failed). Script prints a hint pointing to setup. Proceed frames-only and tell the user.
- **Long video warning printed** → acknowledge it in your answer. Offer to re-run focused on a specific section via `--start`/`--end` rather than a sparse full-video scan.
- **Download fails** → yt-dlp's error goes to stderr. If it's a login-required or region-locked video, tell the user plainly; do not keep retrying.
- **Local ASR (`listen`) fails** → error from `listen` goes to stderr. Common causes: local Qwen3-ASR endpoint unreachable AND `api.scrappylabs.ai` unreachable, or model name mismatch. Retry with `--backend cloud` if a cloud key is configured.
- **Cloud Whisper request fails** → the error is printed to stderr (likely: invalid key, rate limit, or 25 MB upload limit on a very long video). The report will say "none available" for transcript. You can retry with `--backend openai` if Groq failed (or vice versa).

## Token efficiency

This skill burns tokens primarily on frames. Order of magnitude:
- 80 frames at 512px wide is roughly 50-80k image tokens depending on aspect ratio.
- Scene-aware dedup usually lands well under budget on static content (screencasts, slides, talking heads) — expect 2-3× fewer frames than the fixed-interval equivalent with nothing missed.
- The transcript is cheap (a few thousand tokens at most for a 10-minute video).
- Bumping `--resolution` to 1024 roughly quadruples the image tokens per frame. Only do it when necessary.

If you already watched a video this session and the user asks a follow-up, do **not** re-run the script — you already have the frames and transcript in context. Just answer from what you have.

## Security & Permissions

**What this skill does:**
- Runs `yt-dlp` locally to download the video and pull native captions when the source supports them (public data; the request goes directly to whatever host the URL points at)
- Runs `ffmpeg` / `ffprobe` locally to extract frames as JPEGs and, when ASR is needed, a mono 16 kHz audio clip
- When `~/bin/listen` is installed and selected (default), shells out to it with the audio clip. `listen` first tries a local OpenAI-compatible ASR endpoint at `http://127.0.0.1:9877` and, on failure, sends to `https://api.scrappylabs.ai/v1/audio/transcriptions` using the user's configured `SL_ADMIN_API_KEY`
- Sends the extracted audio clip to Groq's Whisper API (`api.groq.com/openai/v1/audio/transcriptions`) when `--backend groq`/`cloud` is selected and `GROQ_API_KEY` is set
- Sends the extracted audio clip to OpenAI's audio transcription API (`api.openai.com/v1/audio/transcriptions`) when `--backend openai` is selected (or as a Groq fallback) and `OPENAI_API_KEY` is set
- Writes the downloaded video, frames, audio, and an intermediate transcript to a working directory under the system temp dir (or `--out-dir` if specified) so Claude can `Read` them
- Reads / creates `~/.config/watch/.env` (mode `0600`) to store cloud API key(s) and a `SETUP_COMPLETE` marker. As a fallback, also reads `.env` in the current working directory

**What this skill does NOT do:**
- Does not upload the video itself to any API — only the extracted audio goes out, and only when native captions are missing AND ASR is not disabled with `--no-asr`
- Does not access any platform account (no login, no session cookies, no posting)
- Does not share API keys between providers (Groq key only goes to `api.groq.com`, OpenAI key only goes to `api.openai.com`, `SL_ADMIN_API_KEY` only goes to `api.scrappylabs.ai` — and only via `~/bin/listen`, not from this script directly)
- Does not log, cache, or write API keys to stdout, stderr, or output files
- Does not persist anything outside the working directory and `~/.config/watch/.env` — clean up the working directory when you're done (Step 5)

**Bundled scripts:** `scripts/watch.py` (entry point), `scripts/download.py` (yt-dlp wrapper), `scripts/frames.py` (ffmpeg frame extraction), `scripts/transcribe.py` (caption parsing + range filtering), `scripts/asr.py` (local-first ASR via `~/bin/listen` + cloud Whisper clients), `scripts/setup.py` (preflight + installer)

Review scripts before first use to verify behavior.
