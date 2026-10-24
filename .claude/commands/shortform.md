---
description: Turn anchor cards into 3:4 portrait shorts (speaker-first framing, fast cuts, the hook on top)
argument-hint: <event-name> <panel> [short ids...]
---
Make shorts for `$ARGUMENTS` (event, panel, optionally short ids). Needs the panel's long-form
story file (`events/<event>/longform/<panel>.story.yaml`) and face tracks.

1. For each anchor I name (or the strongest ones, if I don't), add a spec under `shorts:` in the story
   file: `{id, speaker, anchor, from, to, skip?}`. `from` / `to` are transcript text spanning the
   speaker's *whole* thought: the setup that makes the anchor make sense, the point itself, and a
   payoff line if one follows (a host's reaction, a punchline). No fixed length; never cut mid-idea.
   `skip` lists filler or asides inside the range. Show me the ranges (start and end lines) first.
2. `.venv/bin/python scripts/shortform.py <event> <panel> [--id ID ...] --plan-only` and read the
   captions in `events/<event>/shortform/<id>.ass`: the first line must start the idea, the last must
   finish it, and nothing from the next speaker may leak in.
3. Render: `.venv/bin/python scripts/shortform.py <event> <panel> [--id ID ...]` (~20-40s each).
4. Grab a frame every few seconds of each short and check: speaker framed with eyes on the upper third
   and filling about 2/3 of the frame, the hook clear of faces, captions readable, cuts every ~2-3s.
5. Report per short: path, length, shots, and anything off.

Style: "## Short form" in FORMAT.md (3:4, 1080x1440), on top of the long-form look.
Core tenet: make the output content dynamic.
