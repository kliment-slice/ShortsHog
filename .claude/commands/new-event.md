---
description: Scaffold a new event folder and fill event.yaml
argument-hint: <event-name>
---
Create a new ShortsHog event named `$ARGUMENTS`.

1. If `events/$ARGUMENTS/` already exists, stop and tell me.
2. `cp -R events/_template events/$ARGUMENTS` and remove the `.gitkeep` files in the copy.
3. Set `event: $ARGUMENTS` in its `event.yaml`.
4. Ask me, in one message, for:
   - the event date
   - for each panel: a short id, the master filename, the exact Granola meeting title,
     and each mic file with the speaker's name
   Offer to look up recent Granola meetings (granola MCP) so I can pick titles.
5. Fill `event.yaml` from my answers. Leave `crops: {}` empty; `/cut` fills it.
6. List files already in `masters/`, `mics/`, `broll/` and flag any `event.yaml` path that doesn't exist yet.
7. Tell me which files to drop where, and that the next step is `/transcribe $ARGUMENTS`.
