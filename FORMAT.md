# ShortsHog format

**Core tenet: make the output content dynamic.** Fast cuts, no dead air, no ums,
movement in every shot, PostHog colours on every transition. Someone scrolling past
should recognise a ShortsHog video in the first second.

Rules every short must follow. `/select` applies them; edit freely.

## Frame
- 9:16, 1080x1920
- Frame rate matches the cameras (event.yaml `fps` overrides)

## Length
- Target 30-60s
- Hard max 75s. Anything longer gets split or dropped, never sped up

## Hook
- The first 3 seconds carry the hook
- Start on the strongest line, never on a setup ("So, I think…", "That's a great question")
- If the best line comes mid-answer, start there, as long as the short still makes sense on its own

## Self-contained
- A viewer who saw nothing else understands it
- No back-references: "as he said earlier", "going back to that", "like I mentioned"
- No unresolved pronouns in the first sentence ("they", "that") unless the referent appears in the clip

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

## Captions
Burned in from the whisper words. `scripts/cut.py` reads this block; edit the values.
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
um_min: 0.25        # a pause this long with voice in it (an um whisper didn't write) is muted
um_voice_db: 18     # "voice" = within this many dB of the panel's speech level
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
# --- pacing: dynamic first
shot_max: 5.5               # longest a speaker close-up holds before a cutaway
cutaway_for: 2.2            # wide / group cutaway length
react_for: 1.6              # reaction close-up (someone listening) length
cutaways: [wide, react, group, react]   # cycle of cutaways during an answer
min_shot: 1.6
snap: 1.2                   # move a cut up to this far to land on removed audio or a pause
# --- framing
close_face_mult: 5.0        # close-up height = face height x this (head and shoulders)
max_zoom: 1.8               # 1080p source: never crop smaller than 1/max_zoom of the frame
punch: 1.12                 # jump cut inside a shot: framing steps out (close-ups) or in (wides) by this
push_in: 0.05               # slow zoom over a wide or group shot
# --- PostHog look
grade: "colortemperature=temperature=5600:mix=0.6,eq=contrast=1.14:saturation=1.28:gamma=0.98,colorbalance=rs=-0.03:bs=0.07:rh=0.03:gh=0.03,unsharp=5:5:0.5,vignette=angle=PI/6"
wipe_colors: ["#FF5C1C", "#FFCE1C", "#1490E8"]   # tangerine, lemon, blue; only on theme changes
wipe_seconds: 0.4
title_seconds: 2.8          # title card at the start (0 = none)
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
3:4 portrait shorts from anchor cards (`/shortform`, `scripts/shortform.py`). Same look as the
long form (grade, RoundHog, brand colours, wipe), tighter pacing, speaker-first framing.
```yaml
size: [1080, 1440]
# --- framing: speaker fills ~2/3 of the frame, eyes on the upper third line
close_face_mult: 6.0        # close-up height = face height x this
medium_mult: 1.6            # a looser cutaway on the same speaker = close-up height x this
eye_line: 0.36              # eyes this far down the frame (rule of thirds)
max_zoom: 1.8               # 1080p source: crop never smaller than 1/max_zoom of its height
punch: 1.1                  # jump cut inside a shot: step out by this
# --- pacing: faster than long form
shot_max: 2.8
cutaway_for: 1.4
react_for: 1.2
min_shot: 1.0
snap: 0.8
pad_start: 0.1
pad_end: 0.45
# --- hook (the anchor) at the top, all the way through
hook_y: 54
hook_name_size: 34
hook_size: 54
hook_wrap: 28               # characters per line
# --- captions: a few words at a time, lower third
caption_size: 62
caption_max_chars: 24
caption_margin_v: 210
wipe_seconds: 0.45          # one wipe opens the short
```
