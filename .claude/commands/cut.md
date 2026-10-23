---
description: Build timelines in Resolve from approved shorts.json and render them
argument-hint: <event-name>
---
Cut and render event `$ARGUMENTS` in DaVinci Resolve via the `davinci-resolve` MCP.

Preconditions, check before anything else:
- I approved the `/select` table in this conversation (or explicitly said `shorts.json` is approved). If not, stop and ask.
- Resolve is open with the bridge running (Workspace > Scripts > resolve_bridge). Test with a list-projects call; if it fails, tell me and stop.

If ANY Resolve API call is refused or returns an error: report the tool, the arguments and the
error, then ask me how to proceed. Don't silently work around it, skip the short, or fall back to ffmpeg.

1. **Project**: open `shortshog-$ARGUMENTS`, or create it if it doesn't exist.
2. **Media**: import every master used in `shorts.json`, and all files in `broll/`, into the media pool
   (absolute paths under `events/$ARGUMENTS/`).
3. **fps**: read each master's frame rate (`event.yaml` `fps` overrides; else from the clip's properties,
   or `ffprobe` if Resolve doesn't expose it).
4. **Crops**: for each speaker in `shorts.json` without an entry in that panel's `crops` in `event.yaml`,
   ask me for zoom/pan/tilt (offer to show a frame grab from the master at a moment they speak,
   via ffmpeg into the scratchpad). Store answers in `event.yaml` before cutting.
5. **Per short** (in shorts.json order):
   - Create timeline `<id>` at 1080x1920, timeline fps = master fps.
   - Append the master from `round(in*fps)` to `round(out*fps)` (video + audio).
   - Apply the speaker's static crop to the video item (ZoomX/ZoomY = zoom, Pan, Tilt).
   - If `broll_hint` matches a broll filename, put it on V2 for 2–4s at a natural point
     after the hook (never in the first 3s), audio muted. If no file matches, note it and continue.
6. **Render**: for each timeline, add a render job: H.264 MP4, 1080x1920, timeline fps,
   output `events/$ARGUMENTS/renders/<id>.mp4`. Queue all, then start rendering.
7. Report: per short, timeline created / crop applied / b-roll used / render job queued,
   plus every API refusal. Tell me when renders are done if I ask you to check.
