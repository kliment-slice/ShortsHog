---
description: Write the X post for each rendered short and build the upload sheets (post text, thumbnail, title, description, media settings)
argument-hint: <event> [panel ...] [short ids...]
---
Write posts for `$ARGUMENTS` (event, optionally panels or short ids). The last step of the pipeline, after
`/shortform` has rendered the shorts. Read FORMAT.md "## Social posts" first.

1. **Read each short** before writing about it: its spec (`hook`, `parts`, `anchor`) and its rendered
   captions in `events/<event>/shortform/<id>.ass`. Write only from what the clip actually says.
2. **Write the copy** in PostHog's voice: load the `copy-writing-for-dummies` skill and follow it (James's
   social register for punchy takes). Under each spec in `events/<event>/longform/<panel>.story.yaml`:
   ```yaml
   post:
     text: |-
       The claim, specific, with the clip's number or story.

       Who said it (name, company) and the one detail that makes it land.
     title: ...          # optional: defaults to the short's headline / anchor (70 chars max)
     description: ...    # optional: defaults to "Name, role at Company, on the <panel> panel at <event>."
     thumb_at: 3.0       # optional: seconds into the short for the thumbnail (band open, speaker landed)
   ```
   - Claim first, under 280 characters, no emoji, no hashtags, American English, sentence case titles.
   - Quote only the speaker's exact words, and never stronger than they said it (keep "for us", "I think").
   - No em dashes or middots, no hedge words (helps, empowers, unlock, seamless), no vague quantities
     where the clip has a number.
   - Name the speaker and their company. A PostHog speaker still gets named: the post is about them.
3. Run `.venv/bin/python scripts/post.py <event> [panel ...] [--id ID ...]`. It checks every field against
   X's limits and the house style, checks each video against X's upload limits (length, size, h264/aac),
   renders the caption-free thumbnail (`shortform.py --thumb`: the band open with the name, the speaker
   landed, never an audience cutaway) and writes `events/<event>/posts/<id>.md` plus `posts/index.md`.
   Anything it flags is "not ready": fix it and run again.
4. Look at every thumbnail (`posts/<id>.jpg`): the speaker in frame, eyes open, the band readable.
   Move `thumb_at` where a frame doesn't work.
5. Report per short: the post text, its length, and anything not ready. Never post or schedule anything:
   the sheets are for the user to paste into X by hand. Media settings (category, call to action,
   embedding, downloads, geo) come from event.yaml `social`.
