---
description: Export shorts as FCPXML timelines to edit by hand in DaVinci Resolve
argument-hint: <event-name> [short ids...]
---
Export `$ARGUMENTS` for Resolve (first word: event; further words: only those short ids).
Only export shorts that need manual work; the rest ship as rendered.

1. Run `.venv/bin/python scripts/export_fcpxml.py <event> [--id ID ...]`. Per short it writes to
   `events/<event>/exports/`:
   - `<id>.fcpxml`: 1080x1920 timeline at the event's output fps (default 23.976) with the original
     camera files at full resolution. V1 holds the speaker segments with their crops as transforms,
     V2 the b-roll, A1 the mic mix.
   - `<id>.srt`: captions
   - `<id>.resolve.txt`: the crops as Resolve Inspector values, in case the import drops them
   It marks those shorts `edited-in-resolve` in shorts.json (`--keep-status` to skip that).
2. Tell me how to use it:
   - Resolve: **File > Import > Timeline**, pick the `.fcpxml`. If it asks, don't auto-import the
     source clips' own settings; the media is linked by path.
   - Captions: import the `.srt` into the media pool, drag it onto the timeline (it lands on a
     subtitle track). Style it in Resolve; the burned-in style from FORMAT.md doesn't carry over.
   - If a V1 clip shows the whole camera frame instead of the speaker, the transform was dropped:
     type the Zoom / Position values from `<id>.resolve.txt` into Inspector > Transform.
3. Remind me it's one-way: once a short is edited in Resolve, that version is the final one.
   shortshog doesn't read Resolve edits back, and re-running /cut won't overwrite the Resolve version.
