---
description: Open the local review page for an event's rendered shorts
argument-hint: <event-name>
---
Open the review page for event `$ARGUMENTS`.

1. `events/$ARGUMENTS/shorts.json` must exist (run `/select` first). If some shorts have no
   render yet, say which; the page still lists them.
2. Start `.venv/bin/python scripts/review.py $ARGUMENTS` in the background (default port 8765;
   pass `--port` if it's taken). Open http://127.0.0.1:8765/ in the browser pane.
3. Tell me what the page does: approve / reject, trim in/out (±0.1s / ±1s, sliders, or click a
   word to start there and shift-click to end there), listen to the mic mix at the new in/out,
   edit the hook and caption text, Save (writes shorts.json, keeps shorts.json.bak), Re-render
   (runs cut.py for that short only).
4. Leave the server running until I say I'm done, then stop it. When I'm done, summarise from
   shorts.json: statuses, which shorts changed, and which need `/export` for work in Resolve.
