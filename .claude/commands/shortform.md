---
description: Turn the few anchors that pass the bar into claim-first 9:16 shorts of at most 45s (opening sweep, speaker close-up)
argument-hint: <event-name> <panel> [short ids...]
---
Make shorts for `$ARGUMENTS` (event, panel, optionally short ids). Needs the panel's long-form
story file (`events/<event>/longform/<panel>.story.yaml`) and face tracks.

Quality over quantity: most anchors are not shorts. Read "## Context and accuracy" and "## Short form"
in FORMAT.md first. Structure: claim (the hook) -> context -> evidence (a number, an example, a story) ->
payoff or implication. Cut off-story material hard; never cut the setup a point needs.

1. **Select.** Score every candidate (the panel's anchors, or the ones I name) against the five gates
   in FORMAT.md "### Selection": standalone hot take, concrete, short, not a pitch, no setup needed.
   Short means at most 45s after tightening (target 25-40s), setup and qualifier included, never sped
   up. If it needs more, trim to a tighter self-contained range or reject it: `rejected: too long for 45s`.
   Read the transcript for each, not just the anchor text. Keep at most 2 per panel; 0 is fine.
   Give each rejected spec a one-line `rejected:` reason instead of deleting it.
2. **Spec.** For each pick, write under `shorts:` (all transcript text, exact words):
   - `hook`: the one sentence that works cold, as said: frame 1 is its first word. No "that" or "those"
     pointing at something unsaid, and never stronger than the speaker put it (no hook that drops
     their "for us" or "in some cases").
   - `from` / `to`: the meat. Start where the setup the claim needs starts (not on "So, yeah, I think"
     or the host's question). Read the sentence after `to`: if it qualifies the claim ("but",
     "except", "in our case", "for us"), the range includes it; if that qualifier changes the meaning
     and the short no longer fits in 45s, drop the short. End on the payoff sentence, not a trailing
     "so yeah" and not a reaction. The hook is cut out of the meat unless `repeat_hook: true`.
   - `parts: [{from, to}, ...]` instead of `from` / `to` to stitch several ranges in order (another
     speaker's evidence, or two stretches with an aside between). With `parts` the `hook` is optional:
     without one, frame 1 is the first part's first word. A part that continues a sentence keeps its
     lowercase start; one after a sentence end is capitalised.
   - `swap: [{replace, with}]`: say a word the same speaker said elsewhere in the panel in place of a
     vague one ("they are" -> "agents"), only when it doesn't change the meaning. Listen to it after.
   - `key`: the claim's key words in the opening line (transcript text). The opening sweep lands on the
     speaker before them (by 1.2s at the latest). `sweep: false` turns the sweep off; `sweep_from: <name>`
     picks the seat it starts on (default: the seat furthest from the speaker).
   - `headline` (default: the `anchor`) is the summary band across the top fifth from frame 1;
     `title_key` is the phrase of it on the highlight boxes, `color` the band's brand colour (one of
     FORMAT.md `summary_colors`; the strap takes its darker tone).
   - Audience cutaways come from event.yaml panel `audience` (b-roll filmed during that panel, steady,
     people listening, no phones, crop centre `x`); `audience: false` turns them off for a short.
   - `payoff` (inside the meat): the sentence the short ends on; shortform.py warns if it isn't last.
   - `skip`: pitches, asides or tangents inside the range.
   - Reactions are automatic and only on something heard: another person's words, or their mic clearly
     louder than the speaker's (a laugh, a "yeah"). A pan to them and back, or a hard cut when they sit
     more than 2 seats away. `react: [{at, who, for?}]` hand-picks one instead; `auto_react: false`
     keeps the camera on the speaker throughout.
   If I give times from an older render ("0:13-0:44"), map them to transcript text with that render's
   `.ass` captions (`git show HEAD:events/<event>/shortform/<id>.ass` if it was deleted).
   Show me the picks, the rejects with reasons, and each hook first.
3. **Anchor graphics** (off while FORMAT.md has `overlays: false`: skip this step). Add `beats:` to each pick, about one every 10 to 15 s, in the meat (the hook
   stays clean): `{kind, at, tags, query?, dur?}`. `kind` is `stat`, `punchline` or `topic_shift`; `at`
   is transcript text the graphic lands on; `tags` are 1 to 3 moods or subjects; `query` overrides the
   search terms (write it for motion: "money falling", not "money bag"). Then
   `.venv/bin/python scripts/overlays.py fetch <event> <panel> [--id ID ...]` (Pixabay videos, animations
   first; `--media image` for stills) and look at `events/<event>/overlays/<id>.sheet.jpg`. A green screen
   or plain black background is keyed out automatically, so the clip reads like a GIF; set `key: green`,
   `black` or `none` where the guess is wrong (bills crossing the border, a dark scene). The sheet shows
   each clip's middle frame: if the good part comes later, set `clip_in`. Pick clips that read at a third
   of the width on dark footage. Skip anything resembling a known character or brand. Drop a beat if
   nothing fits.
4. `.venv/bin/python scripts/shortform.py <event> <panel> [--id ID ...] --plan-only` and read the
   captions in `events/<event>/shortform/<id>.ass`: the hook reads alone, the meat starts with the setup,
   the last caption is the payoff, no word lost at a cut, no other speaker leaking in. The plan prints the
   sweep (seats passed, landing time, the key words' time), every reaction pan or cut with its start,
   seats passed and cause, and the length: over 45s is a FAIL and isn't rendered.
   The plan also prints the filler scan: ums whisper's first pass missed, found by a second pass and
   cut (`filler_scan` in "## Short-form edit"). If the first pass dropped real words where a part
   starts (a restart), fix them in `transcripts/<panel>.json` with the second pass's times
   (`transcripts/<panel>.fillers.json`) and a `note`.
5. Render: `.venv/bin/python scripts/shortform.py <event> <panel> [--id ID ...]` (~20-60s each).
   Always captioned. `--clean` also renders `<id>.clean.mp4` with no text on screen at all (no captions,
   title or name strap), only when asked. The audio is the panel's render bed (`transcripts/<panel>.bed.wav` from mix.py:
   one open mic at a time), not the mic sum whisper uses, which echoes.
6. Grab frames every 0.2s through each sweep and each reaction pan: every face it passes goes through
   the frame, the landing has the speaker's eyes on the upper third, no crop tighter than max_zoom, no
   jitter (`<id>.plan.json` `move_frames` has every frame's crop and the faces in view, to check by
   numbers). Also each graphic (off faces, the title and the captions, readable), the name and role at the end of the summary (strap off: `strap: false`), the captions on the lower third line (under the chin,
   never over the mouth), the title (clear of heads) and each audience cutaway (no phones,
   nobody on the panel talking).
   `--replace` places the graphics again.
7. Check the audio: transcribe each render (`whisper-cli -m models/ggml-large-v3-turbo.bin -f <wav>
   --prompt "Um, uh, so, like."`) and compare with the captions: no um left, no word clipped or lost
   at a cut (a lost "not" flips the meaning), any swap heard as intended.
8. Report per short: path, length (anything over 45s is a failure), sweep landing time vs the key
   words, number of reaction pans and cuts, and anything off.

Style: "## Short form" and "## Short-form edit" in FORMAT.md (9:16, 1080x1920), on top of the long-form look.
