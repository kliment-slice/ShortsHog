"""Edit a full panel as a 16:9 video: camera per speaker, zooms, PostHog wipes, captions.

    .venv/bin/python scripts/longform.py <event> <panel> [--start S] [--duration D] [--plan-only]

Shots (style in the "## Long form" block of FORMAT.md):
  close  cam2 cropped on whoever is talking, following their tracked face
         (faces.py track); head and shoulders, never more than max_zoom
  group  cam2 full frame, slow push-in
  wide   cam1 full frame, slow push-in
Who talks comes from the labelled transcript. Each speaker turn opens on a close-up
when their face is tracked there, otherwise on the wide shot (the emcee, people
walking in). Long answers cut away to the wide or group shot every `wide_every`
seconds, at a pause. Cuts between people on cam2 are a short virtual camera move;
cuts between cameras get a PostHog stripe wipe. Captions run along the bottom as
"Name (Company): what they say", in RoundHog, name in the speaker's brand colour.

Frames are composited in Python (both cameras decoded in sync at the output fps)
and piped to ffmpeg with the panel's mic mix and the captions burned in.
Output: events/<event>/longform/<panel>.mp4 (+ .plan.json, .ass).
--start/--duration (panel seconds) render an excerpt: <panel>.<start>-<end>.mp4.
"""
import argparse
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from ruamel.yaml import YAML

from common import REPO, event_dir, ffmpeg_with_libass, load_event, media, output_fps, panel, panel_range, probe, synced

PAUSE = 0.25        # s of silence between words that counts as a place to cut
SNAP = 3.0          # s either side of a planned cut to look for a pause
CUT_LEAD = 0.15     # cut to the next speaker this long before their first word
FACE_Y = 0.40       # face centre sits this far down a close-up


def style() -> dict:
    text = (REPO / "FORMAT.md").read_text()
    m = re.search(r"## Long form.*?```yaml\n(.*?)```", text, re.S)
    if not m:
        sys.exit("FORMAT.md needs a ```yaml block under '## Long form'")
    return YAML(typ="safe").load(m.group(1))


def bgr(hex_rgb: str) -> tuple:
    h = hex_rgb.lstrip("#")
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


def ease(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


# ---------------------------------------------------------------- plan

def turns(words: list, dur: float, min_len: float) -> list:
    """[(speaker, t0, t1)] covering [0, dur]; turns shorter than min_len fold into the previous one."""
    out = []
    for w in words:
        if w.get("silent") or not w.get("speaker"):
            continue
        if not out or out[-1][0] != w["speaker"]:
            out.append([w["speaker"], max(0.0, w["start"] - CUT_LEAD), w["end"]])
        out[-1][2] = w["end"]
    if not out:
        return [(None, 0.0, dur)]
    merged = []
    for spk, a, b in out:
        if merged and (b - a < min_len or merged[-1][0] == spk):
            merged[-1][2] = b
        else:
            merged.append([spk, a, b])
    segs = []
    for i, (spk, a, _) in enumerate(merged):
        end = merged[i + 1][1] if i + 1 < len(merged) else dur
        segs.append((spk, 0.0 if i == 0 else a, end))
    return segs


def pauses(words: list) -> list:
    live = [w for w in words if not w.get("silent")]
    return [(a["end"] + b["start"]) / 2 for a, b in zip(live, live[1:]) if b["start"] - a["end"] >= PAUSE]


def snap(t: float, gaps: list, lo: float, hi: float) -> float:
    near = [g for g in gaps if abs(g - t) <= SNAP and lo < g < hi]
    return min(near, key=lambda g: abs(g - t)) if near else t


class Tracks:
    def __init__(self, path: Path):
        d = json.loads(path.read_text()) if path.exists() else {"t": [], "seats": {}, "width": 1920, "height": 1080}
        self.t = np.array(d["t"])
        self.w, self.h = d["width"], d["height"]
        self.seats = d["seats"]

    def has(self, name: str) -> bool:
        return name in self.seats

    def seen_share(self, name: str, a: float, b: float) -> float:
        if name not in self.seats or not len(self.t):
            return 0.0
        sel = (self.t >= a) & (self.t <= b)
        return float(np.mean(np.array(self.seats[name]["seen"])[sel])) if sel.any() else 0.0

    def at(self, name: str, t: float) -> tuple:
        s = self.seats[name]
        return tuple(float(np.interp(t, self.t, s[k])) for k in ("cx", "cy", "h"))


def plan(words: list, dur: float, tracks: Tracks, st: dict) -> list:
    """[{kind, cam, person, t0, t1}] covering [0, dur]."""
    gaps = pauses(words)
    shots = []
    for spk, a, b in turns(words, dur, st["min_shot"]):
        close = spk is not None and tracks.has(spk) and tracks.seen_share(spk, a, b) >= 0.6
        if not close:
            shots.append({"kind": "wide", "cam": "cam1", "person": spk, "t0": a, "t1": b})
            continue
        t, k = a, 0
        while True:
            nxt = t + st["wide_every"]
            if nxt + st["wide_for"] + st["min_shot"] > b:
                shots.append({"kind": "close", "cam": "cam2", "person": spk, "t0": t, "t1": b})
                break
            c0 = snap(nxt, gaps, t + st["min_shot"], b - st["min_shot"] - st["wide_for"])
            c1 = snap(c0 + st["wide_for"], gaps, c0 + st["min_shot"], b - st["min_shot"])
            shots.append({"kind": "close", "cam": "cam2", "person": spk, "t0": t, "t1": c0})
            away = ("wide", "cam1") if k % 2 == 0 else ("group", "cam2")
            shots.append({"kind": away[0], "cam": away[1], "person": spk, "t0": c0, "t1": c1})
            t, k = c1, k + 1
    merged = []  # neighbours with the same framing become one shot
    for s in shots:
        if merged and (merged[-1]["kind"], merged[-1]["cam"]) == (s["kind"], s["cam"]) and \
                (s["kind"] != "close" or merged[-1]["person"] == s["person"]):
            merged[-1]["t1"] = s["t1"]
        else:
            merged.append(dict(s))
    return merged


# ---------------------------------------------------------------- framing

def full_rect(w: int, h: int, zoom: float) -> tuple:
    cw, ch = w / zoom, h / zoom
    return ((w - cw) / 2, (h - ch) / 2, cw, ch)


def shot_rect(shot: dict, t: float, tracks: Tracks, sizes: dict, st: dict) -> tuple:
    w, h = sizes[shot["cam"]]
    if shot["kind"] == "close":
        cx, cy, fh = tracks.at(shot["person"], t)
        ch = min(h, max(h / st["max_zoom"], fh * st["close_face_mult"]))
        cw = ch * 16 / 9
        x0 = min(max(0.0, cx - cw / 2), w - cw)
        y0 = min(max(0.0, cy - FACE_Y * ch), h - ch)
        return (x0, y0, cw, ch)
    u = (t - shot["t0"]) / max(0.1, shot["t1"] - shot["t0"])
    return full_rect(w, h, 1 + st["push_in"] * u)


def lerp_rect(a: tuple, b: tuple, u: float) -> tuple:
    return tuple(x + (y - x) * u for x, y in zip(a, b))


def warp(src: np.ndarray, rect: tuple, out_w: int, out_h: int) -> np.ndarray:
    x0, y0, cw, ch = rect
    s = out_w / cw
    m = np.float32([[s, 0, -x0 * s], [0, out_h / ch, -y0 * out_h / ch]])
    return cv2.warpAffine(src, m, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def draw_wipe(img: np.ndarray, u: float, colors: list) -> None:
    """PostHog stripes sweeping left to right; the frame is fully covered at u = 0.5."""
    h, w = img.shape[:2]
    band = 0.55 * w
    slant = 0.25 * h
    group = band * len(colors)
    lead = -slant + ease(u) * (w + group + 2 * slant)
    for k, c in enumerate(colors):
        r = lead - k * band
        l = r - band
        if r < 0 or l > w + slant:
            continue
        pts = np.array([[l + slant, 0], [r + slant, 0], [r, h], [l, h]], dtype=np.int32)
        cv2.fillPoly(img, [pts], bgr(c), lineType=cv2.LINE_AA)


# ---------------------------------------------------------------- captions

def ass_color(hex_rgb: str, alpha: float = 1.0) -> str:
    b, g, r = bgr(hex_rgb)
    return f"&H{int(round((1 - alpha) * 255)):02X}{b:02X}{g:02X}{r:02X}"


def ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def caption_lines(words: list, people: dict, st: dict) -> list:
    """[(start, end, speaker, text)] in panel time: phrases that fit one line with the name."""
    lines, cur = [], None
    for w in words:
        if w.get("silent") or not w.get("speaker"):
            continue
        prefix = len(label(w["speaker"], people)) + 2
        if cur is not None:
            gap = w["start"] - cur["end"]
            over = prefix + len(cur["text"]) + 1 + len(w["w"]) - st["caption_max_chars"]
            # one word over is fine if it finishes the sentence (no line starting with "thing.")
            long = over > 0 and not (over <= 12 and re.search(r"[.?!,]$", w["w"]))
            ends = re.search(r"[.?!]$", cur["text"]) and len(cur["text"]) > 25
            if w["speaker"] != cur["speaker"] or gap > 0.8 or long or ends:
                lines.append(cur)
                cur = None
        if cur is None:
            cur = {"speaker": w["speaker"], "start": w["start"], "end": w["end"], "text": w["w"]}
        else:
            cur["text"] += " " + w["w"]
            cur["end"] = w["end"]
    if cur:
        lines.append(cur)
    out = []
    for i, ln in enumerate(lines):
        end = ln["end"] + 0.4
        if i + 1 < len(lines):
            end = min(end, lines[i + 1]["start"]) if lines[i + 1]["start"] - ln["end"] < 0.8 else end
        out.append((ln["start"], max(end, ln["start"] + 0.6), ln["speaker"], ln["text"]))
    return out


def label(name: str, people: dict) -> str:
    company = people.get(name, {}).get("company")
    return f"{name} ({company})" if company else name


def write_ass(path: Path, lines: list, people: dict, colors: dict, st: dict, offset: float) -> None:
    w, h = st["size"]
    box = ass_color(st["caption_box"], st["caption_box_alpha"])
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{st['font_text_weight']},{st['caption_size']},{ass_color(st['caption_text'])},{ass_color(st['caption_text'])},{box},{box},0,0,0,0,100,100,0,0,3,16,0,2,150,150,{st['caption_margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for a, b, spk, text in lines:
        a, b = a - offset, b - offset
        if b <= 0:
            continue
        text = text.replace("\\", "").replace("{", "(").replace("}", ")")
        name = label(spk, people)
        events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Caption,,0,0,0,,"
                      f"{{\\fn{st['font_name_weight']}\\c{ass_color(colors[spk])}}}{name}: "
                      f"{{\\fn{st['font_text_weight']}\\c{ass_color(st['caption_text'])}}}{text}")
    path.write_text(head + "\n".join(events) + "\n")


# ---------------------------------------------------------------- render

class Decoder:
    """Raw BGR frames of one camera from timeline time t0, at the output fps."""

    def __init__(self, ev: dict, cam_id: str, t0: float, dur: float, fps: float, size: tuple):
        cam = synced(ev, cam_id)
        self.w, self.h = size
        file_t = t0 - cam["start"]
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, file_t):.4f}", "-i", str(media(ev, cam["file"])),
             "-t", f"{dur + 1:.3f}", "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self.w * self.h * 3 * 2)
        self.last = np.zeros((self.h, self.w, 3), np.uint8)

    def read(self) -> np.ndarray:
        buf = self.proc.stdout.read(self.w * self.h * 3)
        if len(buf) == self.w * self.h * 3:
            self.last = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
        return self.last

    def close(self):
        self.proc.stdout.close()
        self.proc.wait()


def render(ev: dict, edir: Path, p: dict, start: float, dur: float, plan_only: bool) -> Path:
    st = style()
    out_w, out_h = st["size"]
    fps = output_fps(ev)
    pstart, pend = panel_range(ev, p)
    total = pend - pstart
    dur = min(dur, total - start) if dur else total - start
    words = json.loads((edir / "transcripts" / f"{p['id']}.json").read_text())["words"]
    tracks = Tracks(edir / "faces" / f"{p['id']}.cam2.tracks.json")
    people = {x["name"]: x for x in p.get("people", []) if isinstance(x, dict)}
    order = list(((p.get("seats") or {}).get("cam2")) or []) + [n for n in people if n not in ((p.get("seats") or {}).get("cam2") or [])]
    colors = {n: st["name_colors"][i % len(st["name_colors"])] for i, n in enumerate(order)}
    for w in words:
        if w.get("speaker") and w["speaker"] not in colors:
            colors[w["speaker"]] = st["name_colors"][len(colors) % len(st["name_colors"])]
    shots = plan(words, total, tracks, st)

    out_dir = edir / "longform"
    out_dir.mkdir(exist_ok=True)
    tag = p["id"] if start == 0 and dur >= total - 0.01 else f"{p['id']}.{int(start)}-{int(start + dur)}"
    (out_dir / f"{tag}.plan.json").write_text(json.dumps(shots, indent=1))
    ass = out_dir / f"{tag}.ass"
    write_ass(ass, caption_lines(words, people, st), people, colors, st, start)
    counts = {}
    for s in shots:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    switches = sum(1 for a, b in zip(shots, shots[1:]) if a["cam"] != b["cam"])
    print(f"[{p['id']}] {len(shots)} shots ({', '.join(f'{v} {k}' for k, v in counts.items())}), "
          f"{switches} camera switches, median shot {np.median([s['t1'] - s['t0'] for s in shots]):.1f}s")
    if plan_only:
        return out_dir / f"{tag}.plan.json"

    sizes = {c: (probe(media(ev, synced(ev, c)["file"]))["width"], probe(media(ev, synced(ev, c)["file"]))["height"])
             for c in ("cam1", "cam2")}
    dec = {c: Decoder(ev, c, pstart + start, dur, fps, sizes[c]) for c in ("cam1", "cam2")}
    out = out_dir / f"{tag}.mp4"
    mix = edir / "transcripts" / f"{p['id']}.wav"
    enc = subprocess.Popen(
        [ffmpeg_with_libass(), "-nostdin", "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "-",
         "-ss", f"{start:.4f}", "-t", f"{dur:.4f}", "-i", str(mix),
         "-filter_complex", f"[0:v]subtitles=filename='{str(ass).replace(':', chr(92) + ':')}'[v]",
         "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)],
        stdin=subprocess.PIPE)
    wipe = st["wipe_seconds"]
    move = st["move_seconds"]
    frames = int(round(dur * fps))
    si = 0
    began = time.time()
    try:
        for n in range(frames):
            t = start + n / fps
            src = {c: d.read() for c, d in dec.items()}
            while si + 1 < len(shots) and shots[si + 1]["t0"] <= t:
                si += 1
            shot = shots[si]
            prev = shots[si - 1] if si > 0 else None
            nxt = shots[si + 1] if si + 1 < len(shots) else None
            # Wipes straddle camera switches: the old shot shows until the frame is covered.
            in_wipe_out = nxt is not None and nxt["cam"] != shot["cam"] and t > nxt["t0"] - wipe / 2
            in_wipe_in = prev is not None and prev["cam"] != shot["cam"] and t < shot["t0"] + wipe / 2
            rect = shot_rect(shot, t, tracks, sizes, st)
            if prev is not None and prev["cam"] == shot["cam"] and t < shot["t0"] + move:
                u = ease((t - shot["t0"]) / move)
                rect = lerp_rect(shot_rect(prev, min(t, prev["t1"]), tracks, sizes, st), rect, u)
            frame = warp(src[shot["cam"]], rect, out_w, out_h)
            if in_wipe_out:
                draw_wipe(frame, (t - (nxt["t0"] - wipe / 2)) / wipe, st["wipe_colors"])
            elif in_wipe_in:
                draw_wipe(frame, (t - (shot["t0"] - wipe / 2)) / wipe, st["wipe_colors"])
            enc.stdin.write(frame.tobytes())
            if n and n % int(fps * 60) == 0:
                done = n / frames
                eta = (time.time() - began) / done * (1 - done)
                print(f"[{p['id']}] {n / fps / 60:.0f}/{dur / 60:.0f} min rendered, ~{eta / 60:.0f} min left", flush=True)
    finally:
        enc.stdin.close()
        enc.wait()
        for d in dec.values():
            d.close()
    if enc.returncode:
        sys.exit(f"ffmpeg failed ({enc.returncode})")
    print(f"[{p['id']}] {dur / 60:.1f} min in {(time.time() - began) / 60:.1f} min -> {out.relative_to(edir)}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("panel")
    ap.add_argument("--start", type=float, default=0.0, help="panel seconds")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds (default: to the end)")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    render(ev, edir, panel(ev, args.panel), args.start, args.duration, args.plan_only)


if __name__ == "__main__":
    main()
