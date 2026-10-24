# ShortsHog format

Rules every short must follow. `/select` applies them; edit freely.

## Frame
- 9:16, 1080x1920
- Frame rate matches the cameras (event.yaml `fps` overrides)

## Length
- Target 30–60s
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
- B-roll: 2–4s, never in the first 3s, mic audio keeps playing underneath

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

## Long form
One 16:9 video per panel (`/longform`). Colours are PostHog brand colours
(https://brand.posthog.com/colors), the font is RoundHog (https://brand.posthog.com/fonts).
`scripts/longform.py` reads this block; edit the values.
```yaml
size: [1920, 1080]
font: RoundHog
font_name_weight: RoundHog SemiBold   # speaker name
font_text_weight: RoundHog Medium     # what they say
caption_size: 46            # px
caption_margin_v: 54        # px from the bottom edge
caption_max_chars: 78       # per line, including the name
caption_box: "#151515"      # band behind the text
caption_box_alpha: 0.82
caption_text: "#FFFFFF"
# speaker name colours, given out in seat order
name_colors: ["#FF5C1C", "#FFCE1C", "#1490E8", "#43DAB3", "#A737D2", "#47C861", "#FF474D"]
# stripe wipe on camera switches: tangerine, lemon, blue
wipe_colors: ["#FF5C1C", "#FFCE1C", "#1490E8"]
wipe_seconds: 0.55
move_seconds: 0.6           # virtual camera move between people on the same camera
close_face_mult: 4.0        # close-up height = face height x this (head and shoulders)
max_zoom: 2.4               # never crop smaller than 1/max_zoom of the frame (sharpness)
min_shot: 2.5               # seconds
wide_every: 15              # in a long answer, cut away to a wide or group shot about this often
wide_for: 4                 # seconds the cutaway holds
push_in: 0.06               # slow zoom over a wide or group shot
```
