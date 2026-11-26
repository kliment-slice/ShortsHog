# ShortsHog

Turns PostHog panel recordings into a recognizable, dynamic video format: a long-form 16:9 edit
per panel, then 9:16 portrait shorts from its key moments. Driven by Claude Code slash commands.

## Tools

- **Python 3.12** with NumPy, OpenCV and ruamel.yaml: sync, editing logic, frame compositing
- **FFmpeg**: decoding, mixing, grading, encoding; `ffmpeg-full` (libass) burns in the captions
- **whisper.cpp** (`large-v3-turbo`): word-level transcripts, run on-device
- **OpenCV YuNet**: face detection, so the framing follows whoever is talking
- **Claude Code**: runs the pipeline, maps speakers, writes chapter tags, anchors and cuts
- **Pixabay and Pexels APIs**: stock clips (and stills) for anchor beats in shorts
- **Granola MCP**: panel notes and summaries for picking moments
- **RoundHog** and the PostHog brand colors: captions, cards, transitions
- **DaVinci Resolve** (optional): FCPXML export for hand edits

## Pipeline

```
/new-event      map cameras, mics and panels
/transcribe     sync audio, mix, transcribe, label speakers
/longform       16:9 edit: tight cuts, question tags, anchor cards, captions
/shortform      the few anchors that pass the bar, as hook-first 9:16 shorts
```

Style and pacing live in `FORMAT.md`; how it all fits together is in `CLAUDE.md`.
Run `.venv/bin/python tests/run_synth.py` for the end-to-end test.

## Setup

```bash
brew install ffmpeg ffmpeg-full whisper-cpp uv
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python numpy pyyaml ruamel.yaml opencv-python-headless pillow
curl -L -o models/ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
curl -L -o models/face_detection_yunet_2023mar.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
```

Raw footage goes in `content/<event>/` (not committed). Anchor graphics need free API keys from
[Pixabay](https://pixabay.com/api/docs/) and [Pexels](https://www.pexels.com/api/), in `.env`:

```bash
PIXABAY_API_KEY=...
PEXELS_API_KEY=...
```
