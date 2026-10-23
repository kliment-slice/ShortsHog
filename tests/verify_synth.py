"""Check a rendered synthetic short against truth.json. Usage: verify_synth.py <event dir>"""
import json
import re
import subprocess
import sys

import numpy as np
from pathlib import Path
from ruamel.yaml import YAML

S = sys.argv[1]
R = f"{S}/renders/panel1-01.mp4"
truth = json.load(open(f"{S}/truth.json"))
ev = YAML(typ="safe").load(open(f"{S}/event.yaml"))
short = json.load(open(f"{S}/shorts.json"))[0]
plan = json.load(open(f"{S}/renders/panel1-01.plan.json"))
PSTART = max(min(p["start"] for p in (ev["sources"][m].get("parts") or [ev["sources"][m]])) for m in ev["panels"][0]["mics"])
IN = short["in"]
COLORS = {"red": (255, 0, 0), "yellow": (255, 255, 0), "blue": (0, 0, 255), "green": (0, 128, 0),
          "magenta": (255, 0, 255)}
EXPECT = {"Alice": "red", "Bob": "yellow", "Carol": "blue", "Dave": "green"}
ok = True


def check(cond, line):
    global ok
    ok &= bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {line}")


def frame(t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", R, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)


def color(px):
    return min(COLORS, key=lambda c: sum((int(a) - b) ** 2 for a, b in zip(px, COLORS[c])))


print("Sync offsets:")
for sid, s in ev["sources"].items():
    ps = s.get("parts") or [s]
    for i, part in enumerate(ps):
        t = truth["starts"][sid]
        exp = t[i] if isinstance(t, list) else t
        err = (part["start"] - exp) * 1000
        check(abs(err) <= 1.0, f"{sid}{'' if len(ps) == 1 else f' part {i + 1}'}: {part['start']:+.4f}s vs truth {exp:+.4f}s ({err:+.2f} ms)")

print("Camera per segment (pixel x=540 y=300):")
for s in plan["segments"]:
    for t in (s["t0"] + 0.1, (s["t0"] + s["t1"]) / 2, s["t1"] - 0.1):
        if 3.5 <= t <= 6.5:
            continue
        got = color(frame(t)[300, 540])
        check(got == EXPECT[s["speaker"]], f"t={t:6.2f} {s['speaker']:5} expect {EXPECT[s['speaker']]}, got {got}")

print("Short interjection folded into previous camera:")
check([s["speaker"] for s in plan["segments"]] == ["Alice", "Carol", "Dave", "Alice"],
      " > ".join(s["speaker"] for s in plan["segments"]))

print("B-roll window 3.5-6.5s:")
for t in (3.3, 3.6, 5.0, 6.4, 6.7):
    exp = "magenta" if 3.5 < t < 6.5 else "red"
    got = color(frame(t)[300, 540])
    check(got == exp, f"t={t:4.1f} expect {exp}, got {got}")

print("Camera switch vs true onset:")
for s in plan["segments"][1:]:
    onset = min((u["start"] - PSTART - IN for u in truth["utterances"]
                 if u["speaker"] == s["speaker"] and u["start"] - PSTART - IN > s["t0"] - 1))
    d = s["t0"] - onset
    check(-0.4 <= d <= 0.1, f"cut to {s['speaker']:5} at {s['t0']:6.2f}, speaks at {onset:6.2f} ({d:+.2f}s)")

print("Captions vs true onsets:")
events = []
for m in re.finditer(r"Dialogue: 0,(\d+):(\d+):([\d.]+),(\d+):(\d+):([\d.]+),Default,,0,0,0,,(.*)",
                     open(f"{S}/renders/panel1-01.ass").read()):
    h, mi, se, h2, m2, s2, text = m.groups()
    events.append((int(h) * 3600 + int(mi) * 60 + float(se), int(h2) * 3600 + int(m2) * 60 + float(s2), text))
for u in truth["utterances"]:
    onset = u["start"] - PSTART - IN
    e = min(events, key=lambda e: abs(e[0] - onset))
    first = u["text"].split()[0].lower().strip(".,")
    check(abs(e[0] - onset) <= 0.25 and e[2].lower().startswith(first[:3]),
          f"{u['speaker']:5} speaks {onset:6.2f}, caption '{e[2]}' at {e[0]:6.2f} ({e[0] - onset:+.2f}s)")
wc = [len(e[2].split()) for e in events]
overlaps = sum(1 for a, b in zip(events, events[1:]) if a[1] > b[0] + 1e-6)
check(max(wc) <= 4 and overlaps == 0, f"{len(events)} lines, {min(wc)}-{max(wc)} words, {overlaps} overlaps")
img = frame(events[0][0] + 0.3)
rows = np.nonzero(((img.astype(int) > 230).all(axis=2)).sum(axis=1) > 20)[0]
check(len(rows) and rows.min() >= 1280, f"caption pixels at y={rows.min()}-{rows.max()} (bottom third starts at 1280)")
gaps = [(a[1], b[0]) for a, b in zip(events, events[1:]) if b[0] - a[1] >= 0.2]
if gaps:
    g = (gaps[0][0] + gaps[0][1]) / 2
    rows = np.nonzero(((frame(g).astype(int) > 230).all(axis=2)).sum(axis=1) > 20)[0]
    check(not len(rows), f"no caption on screen in the gap at {g:.2f}s")


def pcm(path, ss, t):
    return np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-ss", str(ss), "-t", str(t), "-i", path,
                                         "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                                        capture_output=True, check=True).stdout, np.float32)


print("Audio continuous across cuts:")
a, b = pcm(R, 0, 20), pcm(f"{S}/transcripts/panel1.wav", IN, 20)
n = min(len(a), len(b))
a, b = a[:n], b[:n]
c = np.correlate(a[8000:64000], b[8000 - 400:64000 + 400], mode="valid")
lag = (int(np.argmax(c)) - 400) / 8
corr = float(np.corrcoef(a, b)[0, 1])
check(abs(lag) <= 2 and corr > 0.95, f"render audio vs mix: lag {lag:+.1f} ms, correlation {corr:.3f}")
fcpxml = Path(S) / "exports" / "panel1-01.fcpxml"
if fcpxml.exists():
    import xml.etree.ElementTree as ET
    from fractions import Fraction
    print("FCPXML round trip (crop and source time rebuilt from the transform):")

    def secs(v):
        return float(Fraction(v.rstrip("s")))

    root = ET.parse(fcpxml).getroot()
    assets = {a.get("id"): a for a in root.iter("asset")}
    formats = {f.get("id"): f for f in root.iter("format")}
    spine = root.find(".//spine")
    for clip, seg in zip(spine.findall("asset-clip"), plan["segments"]):
        asset = assets[clip.get("ref")]
        fmt = formats[asset.get("format")]
        sw, sh = int(fmt.get("width")), int(fmt.get("height"))
        tr = clip.find("adjust-transform")
        scale = float(tr.get("scale").split()[0])
        px, py = (float(v) * 1920 / 100 for v in tr.get("position").split())
        fill = scale * min(1080 / sw, 1920 / sh)
        w, h = 1080 / fill, 1920 / fill
        x, y = sw / 2 - px / fill - w / 2, sh / 2 + py / fill - h / 2
        want = ev["speakers"][seg["speaker"]]["crop"]
        file_t = secs(clip.get("start")) - secs(asset.get("start"))
        good = all(abs(a - b) < 1 for a, b in zip((x, y, w, h), want)) and abs(file_t - seg["camera_time"]) < 0.021
        check(good, f"{clip.get('name'):5} crop [{x:.0f}, {y:.0f}, {w:.0f}, {h:.0f}] vs {want}, "
                    f"source {file_t:.3f}s vs render {seg['camera_time']:.3f}s, at {secs(clip.get('offset')):.3f}s vs {seg['t0']:.3f}s")
    mix = next(c for c in spine.iter("asset-clip") if c.get("lane") == "-1")
    check(abs(secs(mix.get("start")) - IN) < 0.001, f"mic mix starts at {secs(mix.get('start')):.4f}s of the mix vs in {IN}")
    broll = next((c for c in spine.iter("asset-clip") if c.get("lane") == "1"), None)
    if broll is not None:
        parent = next(c for c in spine.findall("asset-clip") if broll in list(c))
        at = secs(parent.get("offset")) + secs(broll.get("offset")) - secs(parent.get("start"))
        check(abs(at - 3.5) < 0.05, f"b-roll on V2 at {at:.3f}s for {secs(broll.get('duration')):.3f}s")

print("ALL CHECKS PASS" if ok else "SOME CHECKS FAILED")
