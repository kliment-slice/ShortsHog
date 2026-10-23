---
description: Shortlist moments from Granola, timestamp them from whisper, write shorts.json
argument-hint: <event-name>
---
Select shorts for event `$ARGUMENTS`. This command ENDS with a table and waits for my approval.

1. Read `FORMAT.md` and `events/$ARGUMENTS/event.yaml`. Every panel needs `transcripts/<panel>.json`
   with speakers filled; if not, tell me to run `/transcribe` first.
2. For each panel, find its Granola meeting by the exact `granola_meeting` title and pull the
   summary, transcript and my notes (granola MCP: `get_meetings`, `get_meeting_transcript`).
   If the title matches zero or several meetings, ask me.
3. Shortlist candidate moments: strong opinions, disagreements, concrete numbers, stories,
   and anything I flagged in my notes (flagged moments get priority).
4. Locate each candidate in the whisper transcript by text, using
   `.venv/bin/python scripts/find_quote.py $ARGUMENTS <panel> "<distinctive phrase>"`.
   It snaps to sentence boundaries and applies 0.2s padding. Read the surrounding
   transcript and adjust in/out to whole sentences when needed.
   - Timestamps come ONLY from whisper. Never use Granola times.
   - Score below ~0.6 means the match is doubtful: check the context or drop the moment.
5. Apply every FORMAT.md rule: 30–60s (hard max 75s), hook in the first 3s on the strongest line,
   self-contained, sentence-boundary cuts. Trim or drop candidates that fail.
6. Rank, keep at most 5 per panel. Write `events/$ARGUMENTS/shorts.json` as
   `[{id, panel, in, out, speaker, hook, why, broll_hint}]` (schema in CLAUDE.md).
   `broll_hint`: a keyword that matches a file in the `broll` folders from `event.yaml`, or null.
7. Print a table: `id | panel | duration | speaker | hook`, then the opening line of each short.

STOP. Do not run `/cut` or render anything. Wait for me to approve or edit.
