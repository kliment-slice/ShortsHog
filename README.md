# ShortsHog

PostHog's signature shorts, from panel recordings to 9:16 renders.

Inputs per event: a synced master per panel (exported from Resolve), raw mic
tracks, b-roll, and the Granola meeting for each panel. Output: 1080x1920 H.264
shorts rendered by DaVinci Resolve (free edition), driven by Claude Code via MCP.

## Workflow
Open Claude Code in this folder, then:

```
/new-event sf-ai-panel-2026     # scaffold + fill event.yaml
                                # drop files into masters/ mics/ broll/
/transcribe sf-ai-panel-2026    # whisper + speaker labels
/select sf-ai-panel-2026        # shortlist -> shorts.json, waits for approval
/cut sf-ai-panel-2026           # Resolve timelines + renders
```

Edit `FORMAT.md` to change the rules shorts are picked and cut by.

## Setup
Already done on this machine; for a fresh one:

```bash
brew install ffmpeg whisper-cpp uv
curl -L -o models/ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python numpy pyyaml
npx davinci-resolve-mcp setup --clients claude-code   # writes .mcp.json into its managed install; copy it here
```

Free-edition Resolve needs the in-app bridge:

```bash
cd "$HOME/Library/Application Support/davinci-resolve-mcp" && venv/bin/python scripts/install_resolve_bridge.py
launchctl setenv PYTHON3HOME "$(python3 -c 'import sys; print(sys.prefix)')"   # resets on reboot
```

Then restart Resolve, open any project, and run **Workspace > Scripts > resolve_bridge**.
Keep it running while Claude works in Resolve.

Granola: `claude mcp add --scope project --transport http granola https://mcp.granola.ai/mcp`,
then authenticate via `/mcp` in Claude Code.
