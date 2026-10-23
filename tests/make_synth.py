"""Build a synthetic ShortsHog event: 2 cameras + 4 mics with known offsets.

    .venv/bin/python tests/make_synth.py <content_dir> <event_dir>

Four speakers (macOS `say` voices) talk in turn, including a 0.45 s interjection.
Each mic hears its speaker plus 15% bleed; mic3 is split into two files mid-sentence.
Cameras carry muffled room audio as scratch sound. Their picture is two flat colours
(cam1 red | yellow, cam2 blue | green), so a correct crop shows exactly one colour:
Alice red, Bob yellow, Carol blue, Dave green; b-roll is magenta.

Timeline: mic1 is the reference (its t=0 = timeline 0). Writes the media into
<content_dir>, and event.yaml + truth.json (known offsets and speech onsets) into <event_dir>.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
SR = 48000
ORIGIN = 10.0  # internal array index 0 = timeline -ORIGIN
TOTAL = 60.0
content = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2]).resolve()
work = content / ".work"
for d in (content / "Audio", content / "B-roll", out, work):
    d.mkdir(parents=True, exist_ok=True)

VOICES = {"Alice": "Samantha", "Bob": "Daniel", "Carol": "Karen", "Dave": "Fred"}
SCRIPT = [
    ("Alice", 6.0, "We deleted forty percent of our dashboards last year, and nobody noticed at all."),
    ("Bob", None, "Right."),
    ("Alice", None, "That told us most of them were never used by anyone."),
    ("Carol", None, "I disagree completely. Dashboards are how new people learn the product."),
    ("Dave", None, "So what would you actually do instead, if you were starting today?"),
    ("Alice", None, "Ship fewer charts, and talk to more users every single week."),
]
GAP = 0.5
# Known file starts on the timeline (timeline second of each file's t=0).
STARTS = {"mic1": 0.0, "mic2": 3.2137, "mic3": -1.7461, "mic4": 0.6029, "cam1": -5.3083, "cam2": 1.9054}
SPLIT = 20.0  # seconds into mic3 where its second file starts
MIC_OF = {"mic1": "Alice", "mic2": "Bob", "mic3": "Carol", "mic4": "Dave"}


def decode(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def write_wav(path, x):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                    "-c:a", "pcm_s16le", str(path)], input=np.clip(x, -1, 1).astype(np.float32).tobytes(),
                   check=True)


n = int((ORIGIN + TOTAL) * SR)
tracks = {s: np.zeros(n, np.float32) for s in VOICES}
truth_utts = []
t = None
for i, (spk, at, text) in enumerate(SCRIPT):
    aiff = work / f"utt{i}.aiff"
    subprocess.run(["say", "-v", VOICES[spk], "-o", str(aiff), text], check=True)
    x = decode(aiff)
    nz = np.nonzero(np.abs(x) > 0.01)[0]
    x = x[nz[0]:nz[-1] + 1]  # trim say's leading/trailing silence so truth times are speech onsets
    t = at if at is not None else t + GAP
    a = int((ORIGIN + t) * SR)
    tracks[spk][a:a + len(x)] += x * 0.6
    truth_utts.append({"speaker": spk, "start": round(t, 3), "end": round(t + len(x) / SR, 3), "text": text})
    t = t + len(x) / SR

rng = np.random.default_rng(0)
room = sum(tracks.values())
for mic, spk in MIC_OF.items():
    sig = tracks[spk] + 0.15 * (room - tracks[spk]) + rng.normal(0, 0.002, n).astype(np.float32)
    a = int((ORIGIN + STARTS[mic]) * SR)
    clip = sig[a:a + int(45 * SR)]
    if mic == "mic3":  # a recorder that splits files: two parts, cut mid-sentence
        write_wav(content / "Audio" / "mic3-1.wav", clip[:int(SPLIT * SR)])
        write_wav(content / "Audio" / "mic3-2.wav", clip[int(SPLIT * SR):])
    else:
        write_wav(content / "Audio" / f"{mic}.wav", clip)

# Camera scratch audio: the whole room, muffled (moving-average lowpass) and noisier.
kernel = np.ones(12, np.float32) / 12
scratch = np.convolve(room, kernel, mode="same") * 0.5 + rng.normal(0, 0.01, n).astype(np.float32)
COLORS = {"cam1": ("red", "yellow"), "cam2": ("blue", "green")}
TIMECODE = {"cam1": "01:00:00:00", "cam2": "10:23:11:07"}  # real cameras embed timecode
for cam, (left, right) in COLORS.items():
    a = int((ORIGIN + STARTS[cam]) * SR)
    wav = work / f"{cam}.wav"
    write_wav(wav, scratch[a:a + int(50 * SR)])
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c={left}:s=640x720:r=25:d=50",
                    "-f", "lavfi", "-i", f"color=c={right}:s=640x720:r=25:d=50",
                    "-i", str(wav), "-filter_complex", "[0][1]hstack[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", "-timecode", TIMECODE[cam],
                    str(content / f"{cam}.mp4")], check=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=magenta:s=1920x1080:r=25:d=6",
                "-c:v", "libx264", "-preset", "ultrafast", str(content / "B-roll" / "clip-dashboard.mp4")],
               check=True)

starts = dict(STARTS)
starts["mic3"] = [STARTS["mic3"], STARTS["mic3"] + SPLIT]
(out / "truth.json").write_text(json.dumps({"starts": starts, "utterances": truth_utts}, indent=1))
rel = os.path.relpath(content, REPO)
(out / "event.yaml").write_text(f"""event: {out.name}
date:
content: {rel}
fps:
reference: mic1
sources:
  cam1: {{file: cam1.mp4, kind: camera}}
  cam2: {{file: cam2.mp4, kind: camera}}
  mic1: {{file: Audio/mic1.wav, kind: mic, speaker: Alice}}
  mic2: {{file: Audio/mic2.wav, kind: mic, speaker: Bob}}
  mic3:
    kind: mic
    speaker: Carol
    parts:
      - {{file: Audio/mic3-1.wav}}
      - {{file: Audio/mic3-2.wav}}
  mic4: {{file: Audio/mic4.wav, kind: mic, speaker: Dave}}
speakers:
  Alice: {{camera: cam1, crop: [0, 0, 405, 720]}}
  Bob: {{camera: cam1, crop: [875, 0, 405, 720]}}
  Carol: {{camera: cam2, crop: [0, 0, 405, 720]}}
  Dave: {{camera: cam2, crop: [875, 0, 405, 720]}}
broll: [B-roll]
panels:
  - id: panel1
    granola_meeting: synthetic
    mics: [mic1, mic2, mic3, mic4]
    start:
    end:
""")
shutil.rmtree(work)
print(f"synthetic event: {out} (media in {content})")
