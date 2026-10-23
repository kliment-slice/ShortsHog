---
description: Scaffold a new event folder and fill event.yaml
argument-hint: <event-name> [content folder, default content/<event-name>]
---
Create a new ShortsHog event from `$ARGUMENTS` (first word: event name; optional second: content folder).

1. If `events/<event-name>/` already exists, stop and tell me.
2. Find the raw footage folder: the given one, else `content/<event-name>`. If neither exists,
   list the folders in `content/` and ask which one.
3. `cp -R events/_template events/<event-name>`, remove the `.gitkeep` files in the copy,
   set `event:` and `content:` in its `event.yaml`.
4. Inventory the content folder (names, not contents; don't decode anything):
   video files with duration and fps (`ffprobe`), audio files with duration, and b-roll folders.
   Propose a mapping from the names: which file looks like each panel's synced master,
   which mics go with which panel, which folders are b-roll.
5. Ask me, in one message, to confirm or correct: the event date, and for each panel a short id,
   the master file, the exact Granola meeting title, and each mic file with the speaker's name.
   Offer to list recent Granola meetings (granola MCP) so I can pick titles.
   If a panel has camera angles but no synced master, say so: the pipeline expects one master per panel.
6. Fill `event.yaml` (paths relative to `content:`). Leave `crops: {}` empty; `/cut` fills it.
7. Flag any `event.yaml` path that doesn't exist. Next step: `/transcribe <event-name>`.
