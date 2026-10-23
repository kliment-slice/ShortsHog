---
description: Render approved shorts with ffmpeg (camera per speaker, mic mix, b-roll, captions)
argument-hint: <event-name> [short ids...]
---
Render event `$ARGUMENTS` (first word: event; any further words: only those short ids).

A full run skips shorts with status `rejected` or `edited-in-resolve` (their final version lives
in Resolve). Only render those if I name them.

Preconditions, check before anything else:
- I approved the `/select` table in this conversation (or explicitly said `shorts.json` is approved). If not, stop and ask.
- Every speaker who talks in an approved short has `camera` and `crop` in `event.yaml`. If not,
  run the crop step from `/new-event` (stills.py) for the missing ones first.

1. **Dry run**: `.venv/bin/python scripts/cut.py <event> [--id ID ...] --dry-run`. It writes
   `renders/<id>.plan.json` and `<id>.ass` without rendering. Show me per short: duration,
   the camera segments (speaker@camera, times) and whether b-roll matched.
   Speaker turns under 1.5s stay on the previous camera; that's intended.
2. **Render**: `.venv/bin/python scripts/cut.py <event> [--id ID ...]` (in the background).
   Per short: speaker segments → that speaker's camera, cropped and scaled to 1080x1920;
   audio is the panel's mic mix, continuous across cuts; b-roll at `broll_hint` cropped to 9:16
   with the mix underneath; captions from the whisper words, styled by FORMAT.md, burned in.
   Output `renders/<id>.mp4`, H.264 crf 20, AAC 192k.
3. Check each output with `ffprobe` (1080x1920, expected duration, has audio) and grab one frame
   per camera segment into the scratchpad to look at framing and captions.
4. Report per short: output path, duration, camera segments, b-roll used or why not, caption
   line count. If ffmpeg or any check fails, show the error. Don't work around it silently.
