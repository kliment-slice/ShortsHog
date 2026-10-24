---
description: Edit each panel as a full 16:9 video (camera per speaker, zooms, PostHog wipes, captions)
argument-hint: <event-name> [panel ids...]
---
Make the long-form edit of `$ARGUMENTS` (first word: event; further words: only those panels).
Needs `/transcribe` done, with people assigned (`speakers.py dump ... --todo` empty).

1. **Seats**: each panel needs `seats.cam2: [names left -> right]` in event.yaml. If missing, find them:
   for each person, grab a cam2 frame from the middle of their longest turn and see who holds the mic
   up and talks (don't identify people by their faces). Show me the frames and the proposed order.
2. **Faces**: `.venv/bin/python scripts/faces.py detect <event> [--panel ID]` (background; ~1 min per
   panel), then `faces.py track <event>` and `faces.py preview <event> <panel>`. Look at every preview:
   each box must sit on the right person in seated frames. If a camera reframe isn't caught
   (positions shift by a seat), add a `framing.cam2` entry with `start` and, if some seat is out of
   frame there, `init` positions. Re-track until the previews are right.
3. **Tighten**: `.venv/bin/python scripts/edit.py <event> <panel>` reports what gets cut and muted
   (ums, long pauses, stutters). Spot-check a few muted gaps if a panel's numbers look off.
4. **Plan**: `.venv/bin/python scripts/longform.py <event> <panel> --plan-only` for each panel; report
   shot counts and camera switches.
5. **Preview**: render 2 minutes (`--start <s> --duration 120`) of one panel, grab frames at a
   close-up, a camera move, a wipe and the captions, and look at them. Fix framing or style first.
6. **Render**: `.venv/bin/python scripts/longform.py <event> <panel>` for each panel, in the background,
   one at a time. Output `events/<event>/longform/<panel>.mp4`.
7. Report per panel: path, duration, shot counts, and anything that looked off.

Core tenet: make the output content dynamic. Pacing, look and tightening are the "## Dynamic edit"
and "## Long form" blocks in FORMAT.md.
