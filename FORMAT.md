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
