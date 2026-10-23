---
description: Scaffold a new event folder, map the footage in event.yaml, set crops
argument-hint: <event-name> [content folder, default content/<event-name>]
---
Create a new ShortsHog event from `$ARGUMENTS` (first word: event name; optional second: content folder).

1. If `events/<event-name>/` already exists, stop and tell me.
2. Find the raw footage folder: the given one, else `content/<event-name>`. If neither exists,
   list the folders in `content/` and ask which one.
3. `cp -R events/_template events/<event-name>`, remove the `.gitkeep` files in the copy,
   set `event:` and `content:` in its `event.yaml`.
4. Inventory the content folder (metadata only, `ffprobe`; don't decode anything):
   video files with duration, fps, resolution and whether they have audio; audio files with
   duration and channels; b-roll folders with file counts.
   Propose a mapping from the names and durations: which videos are cameras, which audio files
   are mics, which mics go with which panel, which folders are b-roll, and a `reference` source
   that overlaps everything (a camera that ran all event is usually best). Flag files whose role is unclear.
5. Ask me, in one message, to confirm or correct: the event date, the camera and mic sources,
   the reference, which speaker is on each mic, and for each panel a short id, its mics and the exact
   Granola meeting title. Offer to list recent Granola meetings (granola MCP) so I can pick titles.
6. Fill `event.yaml`: `sources` (cameras `kind: camera`, mics `kind: mic` + `speaker`), `reference`,
   `broll`, `panels`. Add `sync_to` for any file that won't overlap the reference.
   Add every speaker under `speakers` with `camera` and an empty crop.
7. **Crops**: run `.venv/bin/python scripts/stills.py <event-name>` and show me each
   `stills/<camera>.jpg` (grid every 100 px, heavier every 500 px). Ask me, per speaker,
   which camera and the crop `[x, y, w, h]` in that camera's pixels (9:16, e.g. w = h × 9/16).
   Offer to propose crops from the stills. Write them to `event.yaml`, rerun `stills.py`,
   and show the `stills/<camera>.crops.jpg` files so I can confirm.
8. Flag any `event.yaml` path that doesn't exist. Next step: `/transcribe <event-name>`.
