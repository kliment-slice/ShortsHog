# ShortsHog format

**Core tenet: the speaker's point is the content.** Informative tech talk that holds attention:
no dead air, no ums, the picture changes when the information changes (not on a timer), motion
only on wides; close-ups hold still. PostHog colours on every theme change. No comedic timing.
Someone scrolling past should recognise a ShortsHog video in the first second.

Rules every short must follow. `/select` and `/shortform` apply them; edit freely.

## Frame
- Shorts (`/shortform`): 3:4 portrait, 1080x1440 (the "## Short form" block)
- Legacy `/select` + `/cut` path only: 9:16, 1080x1920
- Frame rate matches the cameras (event.yaml `fps` overrides)

## Length
- `/shortform` (3:4): at most 45s, target 25-40s. Never sped up. If keeping the setup and the
  qualifier needs more than 45s, trim to a tighter self-contained range or reject it
  (`rejected: too long for 45s`). shortform.py refuses to render anything over 45s
- Legacy `/select` + `/cut` (9:16): target 30-60s, hard max 75s

## Hook
- The first 3 seconds carry the hook
- Start on the strongest line, never on a setup ("So, I think…", "That's a great question")
- If the best line comes mid-answer, start there, as long as the short still makes sense on its own

## Self-contained
- A viewer who saw nothing else understands it
- No back-references: "as he said earlier", "going back to that", "like I mentioned"
- No unresolved pronouns in the first sentence ("they", "that") unless the referent appears in the clip

## Context and accuracy
- Cut off-story material hard; never cut the setup a point needs
- A short's range includes the setup that makes the claim understandable, and any qualifier in
  the next sentence ("but", "except", "in our case", "for us"). If the qualifier changes the
  meaning and doesn't fit within the hard max, drop the short
- Anchor, hook and on-screen text never state a claim more strongly than the speaker did
- Structure: claim, then context, then evidence (a number, an example, a story), then the payoff
  or implication. End on the payoff sentence, never on a reaction shot
- The long form keeps panel chronology; its cold open is the only out-of-order clip

## Cuts
- Cut on sentence boundaries only
- 0.2s padding before the first word and after the last word
- Timestamps come from the whisper transcript, never from Granola

## Volume
- Max 5 shorts per panel, ranked best first

## Camera
- Each speaker has one camera and one fixed crop (event.yaml `speakers`)
- The short cuts to the speaker's camera when they start talking
- Speaker turns under 1.5s stay on the previous camera, so it doesn't flicker
- B-roll: 2-4s, never in the first 3s, mic audio keeps playing underneath

## Captions (legacy `/cut` only)
The 9:16 `/select` + `/cut` path's captions. `scripts/cut.py` reads this block. The 3:4 room shorts and the
long form use RoundHog captions from the "## Long form" and "## Short form" blocks instead.
```yaml
font: Helvetica Neue
size: 78                # px on the 1080x1920 frame
bold: true
color: "#FFFFFF"
outline_color: "#000000"
outline: 5              # px
shadow: 0
uppercase: false
margin_v: 460           # px from the bottom edge; ~460 puts lines in the bottom third
margin_h: 80
words_per_line: [2, 4]  # min, max
max_gap: 0.5            # s of silence that forces a new line
hold: 0.3               # s a line stays up after its last word, unless the next line starts
```

## Hook text
- Direct and specific: say the claim, the number, or the disagreement
- No clickbait ("You won't believe…", "This changes everything")
- No exclamation marks

## Dynamic edit
Applies to every output (`scripts/edit.py`). Times in seconds.
```yaml
fillers: [um, uh, uhm, umm, uhh, erm, er, hmm, mm, ah]
filler_cut: 0.30    # fillers at least this long are cut out; shorter ones are muted
pause_max: 0.45     # pauses longer than this are shortened...
pause_keep: 0.22    # ...to this
pause_quiet_db: 10  # a pause cut only takes audio this far under the speech level (whisper's word times can be late)
um_min: 0.25        # a pause this long with voice in it (an um whisper didn't write) is muted
um_voice_db: 18     # "voice" = within this many dB of the panel's speech level
```

## Short-form edit
Overrides "## Dynamic edit" for shorts (`shortform.py`). Every um is cut, never muted, and
pauses go down to a breath: a short has no time to wait.
```yaml
filler_cut: 0       # cut every filler, however short
pause_max: 0.2
pause_keep: 0.08
repeat_max: 4       # restarts up to 4 words long ("we were building, we were building it") are cut
um_cut: true        # an um whisper didn't write (voice in a pause) is cut too, not muted
filler_scan: true   # a second whisper pass over each part, prompted to write ums down: every one it hears is cut
tag_fillers: [you know, i mean, right, like, so, yeah, okay, sort of, kind of, basically, actually]
                    # cut only where they stand alone between punctuation or pauses
```

## Long form
One 16:9 video per panel (`/longform`). Colours are PostHog brand colours
(https://brand.posthog.com/colors), the font is RoundHog (https://brand.posthog.com/fonts).
`scripts/longform.py` reads this block; edit the values.
```yaml
size: [1920, 1080]
font_title_weight: RoundHog Bold
font_name_weight: RoundHog SemiBold   # speaker name
font_text_weight: RoundHog Medium     # what they say
# --- pacing: the picture changes when the information changes, never on a timer
# A cutaway starts only on one of these triggers, and only at a sentence boundary:
#   anchor_end      the anchor sentence finished: a listener's reaction (react_for)
#   new_question    the host starts a new question: wide or group, alternating (cutaway_for)
#   speaker_change  a new speaker: their close-up; a wide first after wide_after_closes close-ups in a row
#   shot_max        a shot has held shot_max and the current sentence has ended
# Inside an anchor sentence nothing cuts away.
cutaway_triggers: [anchor_end, new_question, speaker_change, shot_max]
shot_max: 8.0               # longest a shot holds; it then cuts away at the next sentence end
cutaway_for: 2.0            # wide / group cutaway length (1.5 to 2.2)
react_for: 1.4              # reaction close-up (someone listening) length (1.2 to 1.6)
wide_after_closes: 3        # this many close-ups in a row, then a speaker change opens on the wide
min_shot: 1.6               # also the minimum turn: shorter turns stay on the previous shot
l_cut: 0.25                 # a new speaker's shot starts this far into their first word
# a sentence ends at . ? ! or a change of speaker. Where whisper left a stretch unpunctuated (no
# punctuation within unpunctuated_window words either side), a pause counts as the sentence end:
unpunctuated_window: 6
boundary_pause: 0.6         # s of pause in the source
boundary_capital_pause: 0.25  # s of pause before a capitalised restart ("But", "Because")
# --- framing: close-ups hold still; motion only on wides
close_face_mult: 5.0        # close-up height = face height x this (head and shoulders)
anchor_face_mult: 3.5       # tighter close-up through an anchor sentence (capped by max_zoom)
max_zoom: 1.8               # 1080p source: never crop smaller than 1/max_zoom of the frame
punch: 1.12                 # jump cut inside a shot: framing steps out (close-ups) or in (wides) by this
push_in: 0.03               # slow zoom over a wide or group shot only, never in an anchor sentence
# --- PostHog look
grade: "colortemperature=temperature=5600:mix=0.6,eq=contrast=1.14:saturation=1.28:gamma=0.98,colorbalance=rs=-0.03:bs=0.07:rh=0.03:gh=0.03,unsharp=5:5:0.5,vignette=angle=PI/6"
wipe_colors: ["#FF5C1C", "#FFCE1C", "#1490E8"]   # tangerine, lemon, blue; only on theme changes
wipe_seconds: 0.4
title_seconds: 1.5          # title card after the cold open (0 = none)
cold_open_min: 8            # the cold open (story `cold_open`) plays one anchor sentence, 8 to 15 s
cold_open_max: 15
title_bg: "#FF5C1C"
title_stripes: ["#FFCE1C", "#1490E8", "#0457FF"]
title_text: "#151515"
title_size: 108
progress_height: 8          # tangerine progress bar along the bottom (0 = none)
progress_color: "#FF5C1C"
# --- story: chapter tag per question (top left), anchor popups (top right)
# the content lives in events/<event>/longform/<panel>.story.yaml; each new one is a theme wipe
chapter_label_size: 28
chapter_text_size: 42
chapter_label_color: "#FFCE1C"
anchor_name_size: 32
anchor_text_size: 52
anchor_width: 780           # px; the card's left edge sits this far in from the right margin
anchor_wrap: 28             # characters per line
anchor_hold: 6.5            # seconds on screen (less if the next beat comes sooner)
story_box_alpha: 0.88
# --- captions
caption_size: 46            # px
caption_margin_v: 54        # px from the bottom edge
caption_max_chars: 78       # per line, including the name
caption_box: "#151515"      # band behind the text
caption_box_alpha: 0.82
caption_text: "#FFFFFF"
# speaker name colours, given out in seat order
name_colors: ["#FF5C1C", "#FFCE1C", "#2BB3DF", "#43DAB3", "#A0CA21", "#FF474D", "#FFA81C"]   # bright ones only: readable on the dark band
```

## Short form
3:4 portrait shorts (`/shortform`, `scripts/shortform.py`). 3:4 rather than 9:16: on X the full-screen
player letterboxes it, so its buttons and the post text sit on the bars, not on the summary or captions. Same look as the long form
(grade, RoundHog, brand colours, wipe), speaker-first framing.

### Selection: quality over quantity
Most anchors are not shorts. Someone scrolling past cat videos owes us nothing, so a moment
becomes a short only if it passes **every** gate:
1. **Standalone hot take.** One sentence, said by the speaker, that makes sense and provokes
   with zero context: a claim people could disagree with, a surprising number, a reversal.
   "Bug bounties are dead" passes; "Skills are a low-friction way in" doesn't.
2. **Concrete.** The meat backs the take with a number, a story or a mechanism, not vibes.
3. **Short.** Hook plus meat, with the setup and any qualifier it needs ("## Context and accuracy"),
   fits in 25-40s after tightening, 45s at most. If it needs more, trim to a tighter self-contained
   range or reject it (`rejected: too long for 45s`). Never speed it up.
4. **Not a pitch.** No intros, company descriptions, product plugs or "check out X". Ever.
5. **No setup needed.** Topical to a tech audience without knowing the panel or the speaker.
Then rank and keep **at most 2 per panel**. Zero is a fine answer. Rejected anchors stay in the
long form only.

### Structure: claim, context, evidence, payoff
- Frame 1 is the first word of the hook: the speaker saying the claim (`hook:` in the spec,
  transcript text), exactly as strong as they said it. No title card, no wipe.
- The summary (`headline:`, default `anchor:`, the long form's summary of the point) fills the top
  fifth from frame 1 to the end, like the PostHog thumbnail generator: a solid brand colour, the
  small white logo, the quote in white with `title_key` on soft highlight boxes. Never stronger than
  what's said. Every short has the same band; only the colour (`color:`) changes.
- The speaker's name and role sit in dark text at the end of the summary; no separate name strap.
- Under the hook the picture sweeps across the panel and lands on the speaker (see "### Opening
  sweep"): that is how a short shows the room.
- A hard cut to the meat (`from` .. `to`): the context, the evidence (a number, an example, a story)
  and the payoff or implication. The hook isn't heard twice (it's cut from the meat unless
  `repeat_hook: true`).
- End on the payoff sentence (`payoff:` in the spec), not on a reaction or a trailing "so yeah".

### Opening sweep
- A virtual pan across cam2 (seats.cam2, left to right, and the face tracks; never face recognition).
  It starts on the seated person furthest from the speaker and passes everyone in frame between
  them, then lands on the speaker. Seats out of frame are skipped. With the speaker mid-row, the
  people on the other side aren't passed (one move, no back and forth).
- The hook's audio starts on frame 1; the picture lands by sweep_seconds and before the claim's key
  words (`key:` in the spec, transcript text inside the opening line).
- Cubic ease in-out, one crop size for the whole pan, never smaller than max_zoom. On landing it
  settles with a slight push-in (land_push over land_settle) to the normal close-up.
- Per short: `sweep: false` turns it off, `sweep_from: <name>` picks the start seat.

### Camera: strict
- Stay on the speaker. A close-up is the default and almost always the right shot. Close-ups
  hold still: no push-ins.
- The hook is the anchor sentence: a tighter close-up (`anchor_face_mult`), nothing cuts away.
- The picture changes only on a trigger (`cutaway_triggers`):
  `audible_reaction` someone else is heard (see below); `speaker_change` someone else talks for
  1.5s or more (their shot starts l_cut into their first word, at a sentence boundary);
  `shot_max` the framing has held shot_max and a sentence ends (the framing steps, the speaker
  stays on screen). Jump cuts inside a sentence still punch the framing in or out: that hides
  edit.py's cuts.
- Reactions are heard, never guessed: a laugh, an interjection or a short reply, from another
  person's words in the transcript or from their mic clearly louder than the speaker's
  (react_mic_db, react_mic_min). No reaction on silence. The camera pans to them (react_pan_seconds,
  ease in-out, past anyone in between), holds (react_hold) and pans back as the speaker goes on.
  More than react_pan_max_seats seats away, it's a hard cut instead. Never during the anchor
  sentence (the hook), never within react_after_land of the sweep landing, at most react_max per
  short, never in the last shot. `react: [{at, who, for?}]` hand-picks one (same rules but audibility);
  `auto_react: false` turns them off.
```yaml
size: [1080, 1440]          # 3:4: on X, the full-screen player letterboxes it, so its buttons sit on the bars
# --- framing: speaker fills ~2/3 of the frame, eyes on the upper third line
close_face_mult: 6.0        # close-up height = face height x this
eye_line: 0.42              # eyes this far down the frame: the top fifth is the summary, so the
                            # rule of thirds applies to the picture under it
max_zoom: 1.8               # 1080p source: crop never smaller than 1/max_zoom of its height
anchor_face_mult: 4.5       # tighter close-up through the hook (the anchor sentence), capped by max_zoom
punch: 1.1                  # jump cut inside a shot: step out by this
punch_min: 0.8              # framing only changes at a jump cut this long after the last change
# --- length
max_seconds: 45             # hard max; shortform.py won't render anything longer
target_seconds: [25, 40]
# --- opening sweep: a pan across the seats that lands on the speaker under the hook
sweep: true
sweep_seconds: 1.2          # lands by then (earlier if the spec's `key` words start sooner)
sweep_ease: cubic           # cubic ease in-out (or linear)
land_push: 1.06             # settle on landing: push in by this...
land_settle: 0.5            # ...over this many seconds, to the normal close-up
# --- pacing: triggers only (see "### Camera")
cutaway_triggers: [audible_reaction, speaker_change, shot_max]
shot_max: 4.0               # longest one framing holds; it then steps at the next sentence end
other_min: 1.5              # someone else talking at least this long gets their own shot
# --- reaction pans: only when someone else is heard
react_pan_seconds: 0.4      # pan to the reactor, and back
react_hold: [0.8, 1.2]      # hold on them this long (the reaction's length, clamped)
react_pan_max_seats: 2      # further away than this: a hard cut instead of a pan
react_max: 2                # per short
react_after_land: 3.0       # none in the first seconds after the sweep lands
react_mic_db: -12           # another mic this loud (dB, 0 = its owner talking), 3 dB over the speaker's...
react_mic_min: 0.5          # ...for at least this long counts as a reaction (a laugh, a "yeah")
pad_start: 0.05             # before the meat; the hook starts on its first word, no padding
pad_end: 0.35
# --- summary: the top fifth of every short, like the PostHog thumbnail generator. A solid brand colour
#     (spec `color`, one of summary_colors, default title_color), the small white PostHog logo top left,
#     the headline (spec `headline`, default `anchor`) in quotes, white ExtraBold, left-aligned, the biggest
#     size that fits, the spec's `title_key` on soft white highlight boxes. On from frame 1 to the end;
#     the footage is framed so heads sit below it (eye_line).
summary_height: 0.20        # of the frame height, the same on every short
summary_colors: [green, blue, cobalt, coral, tangerine, purple, violet, corn_blue]   # white text reads on these
title_color: green
summary_logo: assets/posthog-logo-white.png
summary_logo_height: 38
summary_logo_top: 28
summary_logo_gap: 16
summary_margin: 46
summary_weight: ExtraBold
summary_size_max: 64        # capped, so 2- and 3-line quotes look alike
summary_size_min: 38
summary_leading: 1.16
summary_bottom_pad: 22
summary_highlight_alpha: 0.30
summary_highlight_pad: 8
summary_highlight_radius: 10
# --- the band is alive: the footage behind it, blurred and washed in the brand colour (summary_tint_alpha),
#     so movement shows through; it eases into the picture over summary_fade px instead of a hard edge.
#     The quote's words rise in one after another from frame 1, then the highlighter wipes in; the
#     byline follows and leaves at summary_byline_seconds
summary_tint_alpha: 0.65
summary_blur: 32
summary_fade: 36
summary_reveal_stagger: 0.035   # s between one word and the next
summary_reveal_dur: 0.28
summary_reveal_rise: 11         # px
summary_highlight_dur: 0.45
summary_byline_seconds: 5.0     # always gone by then
summary_byline_fade: 0.35
summary_compact_height: 0.15   # then the band closes like a drawer to this, the quote and logo shrinking with it
summary_collapse_dur: 0.5
thumb_at: 3.0              # s: the post thumbnail (shortform.py --thumb): band open with the name, speaker landed
# the quote's lines are balanced: 2 or 3 of them, every line two words or more and at least summary_min_line
# of the longest (no orphans or widows), breaking after punctuation where it can; the biggest size that fits.
# Then "- Name, role at company" in dark text on its own line under the quote: black at an alpha, so it takes
# the band's colour
summary_max_lines: 3
summary_min_line: 0.55
summary_break_penalty: 0.15  # cost of a line break that isn't after a comma or full stop
summary_strict_min: 0.85     # breaks after punctuation win unless their size is under this share of the best
summary_name_size: 30
summary_role_size: 30
summary_name_alpha: 0.72
summary_role_alpha: 0.55
summary_byline_below: 20    # px between the quote and the name under it
# frosted strap settings shared with the long-gone title box
title_blur: 28              # frost: gaussian sigma on the footage behind the strap
title_tint: "#151515"
title_tint_alpha: 0.45
title_border_alpha: 0.18    # a hairline edge on the glass
strap: false                # the summary names the speaker; true brings back the strap below
# --- name strap: a subtle lower third, the name over "role at company" on a small frosted box (the
#     footage blurred and tinted behind it, like the title), no solid colour; centred just under the
#     captions once the sweep has landed, so the opening isn't all at once. Someone else's part gets
#     their own strap as it starts.
strap_name_size: 40
strap_role_size: 30
strap_role_alpha: 0.78      # the role a little quieter than the name
strap_line_gap: 4
strap_pad_x: 22
strap_pad_y: 12
strap_radius: 18
strap_tint_alpha: 0.55        # tinted with the darker tone of the short's colour
strap_gap: 22               # px between the captions and the strap
strap_after_land: 0.4       # s after the sweep lands
strap_seconds: 4.0
strap_fade: 0.35
strap_rise: 14              # px it rises as it fades in
# --- captions: no box, white RoundHog with a black outline, the word being said lit in
#     caption_highlight (event.yaml `caption_highlight` overrides it per event) and grown a touch
#     (caption_pop over caption_pop_ms). On the lower third line: many people watch muted, so they sit
#     where the eye already is, under the speaker's chin, never over the mouth
caption_font: RoundHog Bold
caption_size: 60
caption_max_chars: 22
caption_outline: 6
caption_shadow: 2          # a soft drop under the outline: the line often sits on a light sweater or wall
caption_highlight: lemon
caption_pop: 1.05
caption_pop_ms: 90
caption_margin_v: 450       # px from the bottom to the captions' baseline: the lower third line (2/3 down), clear of X's post text
# --- audience cutaways: the room listening, filmed during this panel (event.yaml panel `audience`:
#     b-roll clip, in point, crop centre), cut in over the speaker's audio at a sentence start, never
#     over the opening, the strap, a graphic or the last seconds. `audience: false` in a spec turns them off
audience_seconds: 1.8
audience_min_short: 18      # shorts shorter than this get none
audience_per: 20            # one per this many seconds of short, at most audience_max
audience_max: 2
audience_after_land: 5.0    # none in the first seconds after the sweep lands
audience_end_room: 4.0      # none in the last seconds
audience_gap: 8.0           # between two cutaways
colors_darker: {lemon: "#F0BC00", blue: "#106FB2", tangerine: "#FF4800", coral: "#F00008", teal: "#25BC95",
                violet: "#512EFF", green: "#35B14E", purple: "#8927AF", cobalt: "#0045D1", lime: "#8BAF1D",
                corn_blue: "#1A89AD", yellow: "#E58E00"}
colors: {lemon: "#FFCE1C", blue: "#1490E8", tangerine: "#FF5C1C", coral: "#FF474D", teal: "#43DAB3",
         violet: "#6D4FFF", green: "#47C861", purple: "#A737D2", cobalt: "#0457FF", lime: "#A0CA21",
         corn_blue: "#2BB3DF", yellow: "#FFA81C"}
wipe_seconds: 0             # no wipe: frame 1 is the hook's first word, a hard cut to the meat
# --- anchor graphics (off for now: overlays: false): one short stock clip per beat (stat, punchline, topic shift), 1 to 3 s, on a
#     rounded card, landing on the beat's words like a visual paragraph break; never over a face, the
#     title, the captions or the strap. Pixabay videos, animations first (`overlays.py fetch`); stills still work
overlays: false            # no stickers or stock clips on the shorts; `beats` stay in the specs
overlay_video_width: 0.36   # of the frame width for a clip (25 to 45%): video needs room to read
overlay_width: 0.20         # of the frame width for a still sticker (18 to 22%)
overlay_dur: 2.0            # seconds on screen (1 to 3)
overlay_fade_in: 0.22       # fade and slide up in (150 to 300 ms)
overlay_fade_out: 0.18
overlay_slide: 28           # px travelled while it fades in
overlay_margin: 40          # px from the frame edge
```

## Social posts
Each short gets a post (`/post`, `scripts/post.py`): a sheet in `events/<event>/posts/<id>.md` laid out in
the order of X's upload form, plus a caption-free thumbnail (`<id>.jpg`, `shortform.py --thumb`). Nothing
is posted automatically: the sheet is for pasting in by hand.

- **Post text** ("What's happening?", spec `post.text`): the speaker's point, claim first, in PostHog's voice
  (the `copy-writing-for-dummies` skill): specific, with the real number or story from the clip, no hedge
  words (helps, empowers, unlock, seamless), no emoji, no hashtags, American English. Quote the speaker
  only with their exact words, and never make the claim stronger than they said it (keep "for us",
  "I think"). Name them and their company. Under 280 characters (a link counts as 23).
- **Title** (spec `post.title`, default the short's `headline` or `anchor`): the claim, sentence case,
  70 characters at most.
- **Description** (spec `post.description`, default "Name, role at Company, at the <panel> panel,
  <event>."): who and where, 200 characters at most.
- **Media settings** from event.yaml `social` (category, call to action, embedding, downloads, geo).
- No em dashes or middots anywhere; `post.py` fails on them, on anything over a limit, and on a video X
  won't take.
```yaml
x_text_max: 280
x_link_len: 23             # X counts every link as this many characters
x_title_max: 70
x_description_max: 200
x_video_max_seconds: 140   # X's limit for most accounts
x_video_max_mb: 512
```

## Tile short
9:16 shorts of one person's tile in a video-call grid recording (`scripts/tileshort.py`, specs under
`tile_shorts:` in the story file). For webinars where there is no room camera to sweep across: the
speaker's tile only, the widest 9:16 crop of it (the recordings are low-res, so never zoomed in further
than a slight push, or the little it takes to centre a speaker who sits low), the face centred, a slow
drift. No crash zooms. The opening is calm: one thing to read at the top, one by the mouth.
- Title: the headline on from frame 1, on a frosted box at the top (the footage blurred and tinted behind
  it), in a heavier RoundHog than the captions, left-aligned, the key phrase on a highlighter bar in the
  short's PostHog colour (one colour per short). It must read as a title, not a subtitle.
- Name: a lower-third strap (name on a dark pill, role on a tab in the short's colour) just under the
  captions for the first few seconds, so the eye catches both.
- Captions: no box, white RoundHog with a black outline, the word being said lit in a PostHog colour.
  Lemon is the default; footage that's already yellow or orange (warm rooms, mustard furniture) sets
  `caption_highlight` in event.yaml to a colour that pops on it. Always below the mouth.
- The tangerine progress bar along the bottom. The call app's own name tag is painted out of the footage.
Edit settings: "## Short-form edit". Length: what the spec asks for, never over max_seconds.
```yaml
size: [1080, 1920]
max_seconds: 75
pad_start: 0.05
pad_end: 0.35
# --- framing: a 9:16 crop of the tile (its full height), the face centred
tile_inset: 6               # px trimmed off each tile edge (the active-speaker border)
push_in: 0.03               # slight push over the whole short (the footage can't take more)
drift: 0.006                # slow float, fraction of the crop size
face_y: 0.5                 # face centre this far down the frame
centre_zoom_max: 1.15       # when the speaker sits low in their webcam, zoom in up to this to keep the face centred
centre_zoom_smooth: 1.5     # s: that zoom moves slowly
sharpen: 0.35               # the tile is upscaled about 4x: a light unsharp mask
vignette: 0.22
# --- title: frosted box at the top, from frame 1
title_weight: ExtraBold
title_size: 64
title_leading: 1.22
title_top: 130              # clear of the platform's top bar
title_margin: 44            # box inset from the frame edges
title_pad: 34
title_radius: 30
title_blur: 28              # frost: gaussian sigma on the footage behind the box
title_tint: "#151515"
title_tint_alpha: 0.36
title_border_alpha: 0.18    # a hairline edge on the glass
title_highlight_pad: 10
title_highlight_radius: 10
# --- name strap: a lower third just under the captions
strap_name_size: 38
strap_role_size: 28
strap_pad_x: 20
strap_pad_y: 10
strap_radius: 10
strap_bg: "#151515"
strap_bg_alpha: 0.92
strap_bottom: 50            # lowest it goes, px from the bottom edge (clear of the progress bar)
strap_gap: 14               # under the captions: it follows them, so the eye catches both
strap_in: 0.5               # s: comes in after the first words, so the opening isn't all at once
strap_seconds: 4.5
strap_fade: 0.35
strap_rise: 16              # px it rises as it fades in
# --- captions: no box, black outline, the word being said in colour, below the mouth
caption_font: RoundHog Bold
caption_size: 62
caption_max_chars: 24
caption_outline: 5
caption_shadow: 0
caption_highlight: lemon    # default; event.yaml `caption_highlight` overrides it per event
mouth_below_face: 0.30      # mouth = face centre + this x face height (YuNet box)
caption_mouth_gap: 120      # px from the mouth to the caption's top (clears the chin)
caption_mouth_pct: 98       # the steady caption position clears the mouth this much of the time...
caption_margin_min: 120     # ...kept in this range (px from the bottom)
caption_margin_max: 420
caption_margin_floor: 60    # a line said while the speaker leans lower still drops to this, never over the lips
progress_height: 8
colors: {lemon: "#FFCE1C", blue: "#1490E8", tangerine: "#FF5C1C", coral: "#FF474D", teal: "#43DAB3",
         violet: "#6D4FFF", green: "#47C861", purple: "#A737D2", cobalt: "#0457FF", lime: "#A0CA21",
         corn_blue: "#2BB3DF", yellow: "#FFA81C"}
```
