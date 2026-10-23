---
description: Whisper-transcribe each master and label words by speaker
argument-hint: <event-name>
---
Transcribe event `$ARGUMENTS`.

1. Check every `master` and mic `file` in `events/$ARGUMENTS/event.yaml` exists (paths are relative to its `content:` folder). If any is missing, stop and list them.
2. Run `.venv/bin/python scripts/transcribe.py $ARGUMENTS` (run in the background; long masters take a while).
   Per master it extracts 16 kHz mono wav, runs whisper-cli (large-v3-turbo, word timestamps via DTW),
   and writes `transcripts/<panel>.json` + `.srt`.
3. Run `.venv/bin/python scripts/align.py $ARGUMENTS`.
   It cross-correlates each mic against its master to get the offset, then assigns each word
   the speaker whose mic has the highest normalised RMS at that moment.
4. Report per panel: word count, mic offsets with confidence, words per speaker.
   Flag any offset with confidence below 8, and any speaker with suspiciously few words.
   Show 3 short excerpts per panel with speaker labels so I can spot-check.
