---
description: Sync cameras and mics, mix each panel, transcribe, label words by speaker
argument-hint: <event-name>
---
Transcribe event `$ARGUMENTS`. Run each step in the background if it may take more than ~2 minutes.

1. Check every source `file` in `events/$ARGUMENTS/event.yaml` exists (relative to its `content:` folder).
   If any is missing, stop and list them.
2. **Sync**: `.venv/bin/python scripts/align.py sync $ARGUMENTS` (add `--force` to redo sources that
   already have a `start`). Puts every camera and mic on the shared timeline via audio cross-correlation
   and writes `start`, `duration`, `sync_peak` into `event.yaml`.
   Report each start. If any `sync_peak` is below 1.3, stop and tell me before going on.
3. **Mix**: `.venv/bin/python scripts/mix.py $ARGUMENTS` → `transcripts/<panel>.wav`, a plain sum of
   each panel's `mix_mics` (default: its `mics`), loudness-normalised. Leave a dead mic out of
   `mix_mics`: it only hears the room and makes the mix echo. Ask me to listen before transcribing.
4. **Transcribe**: `.venv/bin/python scripts/transcribe.py $ARGUMENTS` → whisper (large-v3-turbo,
   DTW word timing) on each mix → `transcripts/<panel>.json` + `.srt`.
5. **Label**: `.venv/bin/python scripts/align.py label $ARGUMENTS` → each word gets the speaker whose mic
   is loudest at that moment; words with no mic energy are marked `silent` (whisper hallucinations).
6. **People**: mic labels say which mic, not who held it. For each panel, run
   `.venv/bin/python scripts/speakers.py dump $ARGUMENTS <panel>` and assign every sentence to a person
   from the panel's `people` by what they say: the host asks the questions and moves things on, each
   panelist talks about their own company. Mic labels are a strong hint (one person per mic unless it
   was shared). Write `[[first_id, last_id, "Name"], ...]` to a scratch file and run
   `speakers.py apply $ARGUMENTS <panel> <file>`; it's saved as `transcripts/<panel>.speakers.json`
   and `align.py label` keeps it. Then cross-check: each person should land almost entirely on one mic.
   Show me the assignment per panel and flag the lines you weren't sure about.
7. Report per panel: range on the timeline and duration, word count, words per speaker, silent words.
   Flag any speaker with suspiciously few words. Show 3 short excerpts per panel with speaker
   labels so I can spot-check.
