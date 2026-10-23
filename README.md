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
/review oss-week                       # browser: approve/reject, trim, fix captions, re-render
/export oss-week oss-03                # only shorts that need hand editing -> Resolve
```

**Render → review → export only what needs manual work.** Most fixes (a late start,
a caption typo, a short that doesn't land) happen in `/review` and re-render in
seconds. `/export` is for what the page can't do: re-framing a shot, swapping a
camera, placing b-roll by eye, grading. Export is one-way: once a short is edited
in Resolve, that version is final and shortshog doesn't read it back.

Edit `FORMAT.md` to change the rules shorts are picked and cut by, and the caption style.

## Editing in Resolve
`/export` writes `events/<event>/exports/<id>.fcpxml`: a 1080x1920 timeline that
references the original camera files at full resolution, with the speaker
segments and their crops on V1, b-roll on V2 and the mic mix on A1.
In Resolve (free works): **File > Import > Timeline** → the `.fcpxml`. Captions
come as `<id>.srt`: import it into the media pool and drag it onto the timeline.
If a clip shows the whole camera frame, Resolve dropped the transform: the
Zoom / Position values for each clip are in `<id>.resolve.txt`.

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
| `review.py` | local review page (127.0.0.1:8765): status, trim, captions, re-render |
| `export_fcpxml.py` | FCPXML + SRT per short for hand editing in Resolve |

## Tests
`.venv/bin/python tests/run_synth.py` builds a synthetic event with known answers
(2 cameras with timecode, 4 mics with bleed, one split into two files) and checks
sync, speaker labels, camera switches, b-roll, captions, audio and the FCPXML export.

## Setup
Already done on this machine; for a fresh one:

```bash
brew install ffmpeg ffmpeg-full whisper-cpp uv   # ffmpeg-full (keg-only) has libass for captions
curl -L -o models/ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python numpy pyyaml ruamel.yaml
claude mcp add --scope project --transport http granola https://mcp.granola.ai/mcp
```

Then in Claude Code here: approve the `granola` server and sign in via `/mcp`.

## Legacy: Resolve automation
Resolve is now only an editor you open by hand (see above). An earlier version drove
it through the DaVinci Resolve MCP and its free-edition in-app bridge. That isn't used
any more; see the legacy note in `CLAUDE.md` for what's still installed. To reinstall the bridge:

```bash
npx davinci-resolve-mcp setup --clients claude-code
cd "$HOME/Library/Application Support/davinci-resolve-mcp" && venv/bin/python scripts/install_resolve_bridge.py
launchctl setenv PYTHON3HOME "$(python3 -c 'import sys; print(sys.prefix)')"   # resets on reboot
```
