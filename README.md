# ShortsHog

PostHog's signature shorts, from panel recordings to captioned 9:16 renders.

Inputs per event: camera angles (with their scratch audio), raw mic tracks,
b-roll, and the Granola meeting for each panel. No synced master needed: every
file is lined up by audio cross-correlation. Output: 1080x1920 H.264 shorts
that cut to whoever is speaking, with burned captions. Everything renders
locally with ffmpeg, driven by Claude Code.

## Workflow
Drop raw footage into `content/<event>/`, open Claude Code in this folder, then:

```
/new-event oss-week content/OSS_WEEK   # map cameras/mics/panels, set crops from stills
/transcribe oss-week                   # sync -> mic mix -> whisper -> speaker labels
/select oss-week                       # shortlist -> shorts.json, waits for approval
/cut oss-week                          # render renders/<id>.mp4
```

Edit `FORMAT.md` to change the rules shorts are picked and cut by, and the caption style.

## Scripts
| Script | Does |
|---|---|
| `align.py sync` | puts every camera and mic on one timeline (`start` per source in event.yaml) |
| `mix.py` | per panel: gain-matched, loudness-normalised mix of its mics → `transcripts/<panel>.wav` |
| `transcribe.py` | whisper on the mix, word timestamps → `transcripts/<panel>.json` + `.srt` |
| `align.py label` | speaker per word from mic levels; flags hallucinated words in silence |
| `find_quote.py` | finds a quote in the transcript, snapped to sentences, padded |
| `stills.py` | frame grab per camera with a grid, and with crops drawn once set |
| `cut.py` | renders shorts: camera per speaker, mix audio, b-roll, ASS captions |

## Setup
Already done on this machine; for a fresh one:

```bash
brew install ffmpeg ffmpeg-full whisper-cpp uv   # ffmpeg-full (keg-only) has libass for captions
curl -L -o models/ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python numpy pyyaml ruamel.yaml
claude mcp add --scope project --transport http granola https://mcp.granola.ai/mcp
```

Then in Claude Code here: approve the `granola` server and sign in via `/mcp`.

## Legacy: DaVinci Resolve
An earlier version rendered through the DaVinci Resolve MCP and its free-edition
in-app bridge. It isn't used any more; see the legacy note in `CLAUDE.md` for what's
still installed. To reinstall the bridge:

```bash
npx davinci-resolve-mcp setup --clients claude-code
cd "$HOME/Library/Application Support/davinci-resolve-mcp" && venv/bin/python scripts/install_resolve_bridge.py
launchctl setenv PYTHON3HOME "$(python3 -c 'import sys; print(sys.prefix)')"   # resets on reboot
```
