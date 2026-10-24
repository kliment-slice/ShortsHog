"""Edit a full panel as a dynamic 16:9 video in the ShortsHog look.

    .venv/bin/python scripts/longform.py <event> <panel> [--start S] [--duration D] [--plan-only]

Core tenet: make the output content dynamic. Style and pacing live in FORMAT.md
("## Dynamic edit" and "## Long form").

  1. Tighten (edit.py): ums muted, long pauses / long ums / stutters cut. Everything
     below runs on that tightened timeline; the cameras skip what was cut.
  2. Shots, by who's talking (labelled transcript): a close-up on the speaker
     (cam2, following their tracked face), with cutaways every few seconds that cycle
     through the cam1 wide, a reaction close-up of someone listening, and the cam2
     group shot. Cuts prefer the points where audio was removed, which hides them.
  3. Cuts are hard cuts. The PostHog stripe wipe marks a theme change only: a new
     question or a new anchor from the panel's story file. A jump cut inside a shot
     (a removed pause) punches the framing in or out a little.
  4. Story (events/<e>/longform/<panel>.story.yaml, panel time): a chapter tag for the
     current question, top left, and anchor cards that pop up top right with a
     speaker's key point. Clean dark cards; on-screen text never uses middots or dashes.
  5. The look: camera footage graded per `grade` (cam1 also levelled by its `rotate`);
     a title card that wipes into the panel; RoundHog captions "Name (Company): ..."
     with the name in the speaker's brand colour; a thin progress bar in tangerine.

Frames are composited in Python and piped to ffmpeg with the tightened audio and
captions. Output: events/<event>/longform/<panel>.mp4 (+ .plan.json, .ass).
--start/--duration are seconds of the *output*; an excerpt is <panel>.<start>-<end>.mp4.
"""
import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
import time
from bisect import bisect_right
from pathlib import Path

import cv2
import numpy as np
from ruamel.yaml import YAML

import edit as tighten
from common import REPO, event_dir, ffmpeg_with_libass, load_event, media, output_fps, panel, panel_range, probe, synced

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
    return [(spk, 0.0 if i == 0 else a, merged[i + 1][1] if i + 1 < len(merged) else dur)
            for i, (spk, a, _) in enumerate(merged)]


class Tracks:
    """Face tracks (source panel time) from faces.py."""

    def __init__(self, path: Path):
        d = json.loads(path.read_text()) if path.exists() else {"t": [], "seats": {}, "width": 1920, "height": 1080}
        self.t = np.array(d["t"])
        self.seats = d["seats"]

    def has(self, name) -> bool:
        return name in self.seats

    def seen_share(self, name, a: float, b: float) -> float:
        if name not in self.seats or not len(self.t):
            return 0.0
        sel = (self.t >= a - 0.5) & (self.t <= b + 0.5)
        return float(np.mean(np.array(self.seats[name]["seen"])[sel])) if sel.any() else 0.0

    def at(self, name, t: float) -> tuple:
        s = self.seats[name]
        return tuple(float(np.interp(t, self.t, s[k])) for k in ("cx", "cy", "h"))


def plan(words: list, dur: float, tracks: Tracks, st: dict, chops: list, to_src, hosts: set) -> list:
    """[{kind, cam, person, speaker, t0, t1}] on the output timeline, covering [0, dur]."""
    live = words
    pauses = [(a["end"] + b["start"]) / 2 for a, b in zip(live, live[1:]) if b["start"] - a["end"] >= 0.15]
    points = sorted(set([round(c, 3) for c in chops] + [round(p, 3) for p in pauses]))
    chop_set = set(round(c, 3) for c in chops)

    def snap(t, lo, hi):
        """Nearest cut point to t within snap seconds, preferring removed-audio points; else t."""
        i = bisect_right(points, t)
        near = [p for p in points[max(0, i - 40):i + 40] if abs(p - t) <= st["snap"] and lo < p < hi]
        if not near:
            return t
        return min(near, key=lambda p: abs(p - t) - (0.4 if p in chop_set else 0))

    def seen(name, a, b):
        return tracks.seen_share(name, to_src(a), to_src(b))

    shots, k, last_speaker = [], 0, None
    for spk, a, b in turns(words, dur, st["min_shot"]):
        close_ok = (spk is not None and tracks.has(spk) and seen(spk, a, b) >= 0.6
                    and seen(spk, a, min(b, a + 10)) >= 0.75)
        t = a
        while t < b - 1e-3:
            nxt = t + st["shot_max"]
            if nxt + st["cutaway_for"] + st["min_shot"] > b:
                shots.append({"kind": "close" if close_ok else "wide", "cam": "cam2" if close_ok else "cam1",
                              "person": spk if close_ok else None, "speaker": spk, "t0": t, "t1": b})
                break
            c0 = snap(nxt, t + st["min_shot"], b - st["min_shot"] - st["cutaway_for"])
            shots.append({"kind": "close" if close_ok else "wide", "cam": "cam2" if close_ok else "cam1",
                          "person": spk if close_ok else None, "speaker": spk, "t0": t, "t1": c0})
            kind = st["cutaways"][k % len(st["cutaways"])]
            k += 1
            who = None
            if kind == "react":
                cands = [n for n in tracks.seats if n != spk and seen(n, c0, c0 + st["react_for"]) >= 0.8]
                pref = [n for n in cands if (n in hosts) != (spk in hosts)] or \
                       [n for n in cands if n == last_speaker] or cands
                who = pref[k % len(pref)] if pref else None
                if who is None:
                    kind = "wide"
            hold = st["react_for"] if kind == "react" else st["cutaway_for"]
            c1 = snap(c0 + hold, c0 + st["min_shot"] * 0.6, b - st["min_shot"])
            if not close_ok and kind in ("wide", "react"):
                kind = "group" if shots[-1]["kind"] == "wide" else "wide"
                who = None
            cam = "cam1" if kind == "wide" else "cam2"
            shots.append({"kind": kind, "cam": cam, "person": who, "speaker": spk, "t0": c0, "t1": c1})
            t = c1
        last_speaker = spk
    merged = []
    for s in shots:
        if s["t1"] - s["t0"] < 0.05:
            continue
        if merged and (merged[-1]["kind"], merged[-1]["cam"], merged[-1]["person"]) == (s["kind"], s["cam"], s["person"]):
            merged[-1]["t1"] = s["t1"]
        else:
            merged.append(dict(s))
    return merged


def load_story(path: Path) -> dict:
    if not path.exists():
        return {"questions": [], "anchors": [], "cuts": []}
    d = YAML(typ="safe").load(path.read_text()) or {}
    return {"questions": d.get("questions") or [], "anchors": d.get("anchors") or [], "cuts": d.get("cuts") or []}


def split_at(shots: list, times: list) -> list:
    """Make sure a shot starts at each theme time, so its wipe hides a cut."""
    for w in sorted(times):
        for i, s in enumerate(shots):
            if s["t0"] < w < s["t1"]:
                if w - s["t0"] >= 0.4 and s["t1"] - w >= 0.4:
                    second = {**s, "t0": w, "flip": not s.get("flip", False)}
                    s["t1"] = w
                    shots.insert(i + 1, second)
                break
    return shots


# ---------------------------------------------------------------- framing and graphics

def full_rect(w: int, h: int, zoom: float) -> tuple:
    cw, ch = w / zoom, h / zoom
    return ((w - cw) / 2, (h - ch) / 2, cw, ch)


def shot_rect(shot: dict, T: float, t_src: float, punched: bool, tracks: Tracks, sizes: dict, st: dict) -> tuple:
    w, h = sizes[shot["cam"]]
    if shot["kind"] in ("close", "react"):
        cx, cy, fh = tracks.at(shot["person"], t_src)
        ch = min(h, max(h / st["max_zoom"], fh * st["close_face_mult"]))
        if punched:  # jump cut inside a close-up: step out a little (never tighter than max_zoom)
            ch = min(h, ch * st["punch"])
        cw = ch * 16 / 9
        if cw > w:
            cw, ch = w, w * 9 / 16
        x0 = min(max(0.0, cx - cw / 2), w - cw)
        y0 = min(max(0.0, cy - FACE_Y * ch), h - ch)
        return (x0, y0, cw, ch)
    u = (T - shot["t0"]) / max(0.1, shot["t1"] - shot["t0"])
    zoom = (1 + st["push_in"] * u) * (st["punch"] if punched else 1.0)
    return full_rect(w, h, zoom)


def warp(src: np.ndarray, rect: tuple, out_w: int, out_h: int) -> np.ndarray:
    x0, y0, cw, ch = rect
    m = np.float32([[out_w / cw, 0, -x0 * out_w / cw], [0, out_h / ch, -y0 * out_h / ch]])
    return cv2.warpAffine(src, m, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def draw_wipe(img: np.ndarray, u: float, colors: list) -> None:
    """PostHog stripes sweeping left to right; the frame is fully covered at u = 0.5."""
    h, w = img.shape[:2]
    band = 0.55 * w
    slant = 0.25 * h
    lead = -slant + ease(u) * (w + band * len(colors) + 2 * slant)
    for k, c in enumerate(colors):
        r = lead - k * band
        l = r - band
        if r < 0 or l > w + slant:
            continue
        pts = np.array([[l + slant, 0], [r + slant, 0], [r, h], [l, h]], dtype=np.int32)
        cv2.fillPoly(img, [pts], bgr(c), lineType=cv2.LINE_AA)


def title_card(w: int, h: int, st: dict) -> np.ndarray:
    """Tangerine card with brand stripes down the right; the text is drawn by the captions (RoundHog)."""
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = bgr(st["title_bg"])
    slant = 0.25 * h
    band = 0.06 * w
    x = w * 0.82 - slant / 2  # clear of the title text, which ends well before 0.82 w at the top
    for c in st["title_stripes"]:
        pts = np.array([[x + slant, 0], [x + slant + band, 0], [x + band, h], [x, h]], dtype=np.int32)
        cv2.fillPoly(img, [pts], bgr(c), lineType=cv2.LINE_AA)
        x += band
    return img


def draw_progress(img: np.ndarray, frac: float, st: dict) -> None:
    h, w = img.shape[:2]
    bh = st["progress_height"]
    track = img[h - bh:h]
    track[:] = (track * 0.45).astype(np.uint8)
    img[h - bh:h, :int(w * min(1.0, frac))] = bgr(st["progress_color"])


# ---------------------------------------------------------------- captions

def ass_color(hex_rgb: str, alpha: float = 1.0) -> str:
    b, g, r = bgr(hex_rgb)
    return f"&H{int(round((1 - alpha) * 255)):02X}{b:02X}{g:02X}{r:02X}"


def ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def label(name: str, people: dict) -> str:
    company = people.get(name, {}).get("company")
    return f"{name} ({company})" if company else name


def caption_lines(words: list, people: dict, st: dict) -> list:
    """[(start, end, speaker, text)]: phrases that fit one line with the name."""
    lines, cur = [], None
    for w in words:
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
        if i + 1 < len(lines) and lines[i + 1]["start"] - ln["end"] < 0.8:
            end = min(end, lines[i + 1]["start"])
        out.append((ln["start"], max(end, ln["start"] + 0.6), ln["speaker"], ln["text"]))
    return out


def wrap(text: str, width: int) -> list:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return lines + ([cur] if cur else [])


def clean(text: str) -> str:
    """House style for anything drawn on screen: no middots, no em or en dashes, "I" capitalised."""
    text = re.sub(r"\bi(?=$|\s|'(?:m|ve|ll|d|s)\b)", "I", text)
    text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text)
    text = re.sub(r"\s*\u00b7\s*", ", ", text)
    return re.sub(r",\s*,", ",", text).strip(" ,")


def story_events(story: dict, to_out, people: dict, colors: dict, st: dict, offset: float, end: float) -> list:
    """ASS events: chapter tag per question (top left), anchor cards (top right)."""
    ev = []
    qs = sorted(story["questions"], key=lambda q: q["t"])
    total_q = sum(1 for q in qs if q.get("n"))
    times = sorted([to_out(q["t"]) for q in qs] + [to_out(a["t"]) for a in story["anchors"]])
    lab, txt = st["chapter_label_size"], st["chapter_text_size"]
    for i, q in enumerate(qs):
        a = to_out(q["t"]) - offset
        b = min((to_out(qs[i + 1]["t"]) if i + 1 < len(qs) else end), end) - offset
        if b <= 0 or a >= b:  # outside this render (an excerpt); an event ending before it starts breaks libass
            continue
        t0, t1 = ass_time(max(0.0, a + 0.05)), ass_time(b)
        tag = clean(q.get("label") or (f"QUESTION {q['n']}/{total_q}" if q.get("n") else ""))
        fade = "\\fad(180,0)"
        ev.append(f"Dialogue: 2,{t0},{t1},Chapter,,0,0,0,,{{\\an7\\pos(60,44){fade}\\fn{st['font_title_weight']}"
                  f"\\fs{lab}\\c{ass_color(st['chapter_label_color'])}}}{tag.upper()}")
        ev.append(f"Dialogue: 2,{t0},{t1},Chapter,,0,0,0,,{{\\an7\\pos(60,{44 + lab + 22}){fade}}}{clean(q['text'])}")
    name_size, text_size = st["anchor_name_size"], st["anchor_text_size"]
    for an in story["anchors"]:
        a = to_out(an["t"]) - offset
        later = [t for t in times if t > to_out(an["t"]) + 0.01]
        b = min([a + st["anchor_hold"], end - offset] + [t - offset for t in later[:1]])
        if b <= 0 or a >= b:
            continue
        t0, t1 = ass_time(max(0.0, a + 0.05)), ass_time(b)
        spk = an["speaker"]
        lines = wrap(f"\u201c{clean(an['text'])}\u201d", st["anchor_wrap"])
        x, y = st["size"][0] - 90 - st["anchor_width"], 44
        pop = ("\\fad(160,220)\\fscx88\\fscy88\\t(0,170,\\fscx104\\fscy104)\\t(170,280,\\fscx100\\fscy100)")
        height = name_size + 26 + len(lines) * (text_size + 14)
        name = label(spk, people)
        ev.append(f"Dialogue: 3,{t0},{t1},Anchor,,0,0,0,,{{\\an7\\pos({x},{y}){pop}\\fn{st['font_name_weight']}"
                  f"\\fs{name_size}\\c{ass_color(colors.get(spk, st['name_colors'][0]))}}}{name}")
        for k, ln in enumerate(lines):
            ev.append(f"Dialogue: 3,{t0},{t1},Anchor,,0,0,0,,{{\\an7\\pos({x},{y + name_size + 26 + k * (text_size + 14)}){pop}}}{ln}")
    return ev


def write_ass(path: Path, lines: list, people: dict, colors: dict, st: dict, offset: float,
              title: tuple | None, story: list = ()) -> None:
    w, h = st["size"]
    box = ass_color(st["caption_box"], st["caption_box_alpha"])
    sbox = ass_color(st["caption_box"], st["story_box_alpha"])
    dark = ass_color(st["title_text"])
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{st['font_text_weight']},{st['caption_size']},{ass_color(st['caption_text'])},{ass_color(st['caption_text'])},{box},{box},0,0,0,0,100,100,0,0,3,16,0,2,150,150,{st['caption_margin_v']},1
Style: Title,{st['font_title_weight']},{st['title_size']},{dark},{dark},{dark},{dark},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Chapter,{st['font_name_weight']},{st['chapter_text_size']},{ass_color(st['caption_text'])},{ass_color(st['caption_text'])},{sbox},{sbox},0,0,0,0,100,100,0,0,3,14,0,7,0,0,0,1
Style: Anchor,{st['font_title_weight']},{st['anchor_text_size']},{ass_color(st['caption_text'])},{ass_color(st['caption_text'])},{sbox},{sbox},0,0,0,0,100,100,0,0,3,16,0,7,0,0,0,1
Style: Story,{st['font_name_weight']},20,{ass_color(st['caption_text'])},{ass_color(st['caption_text'])},{sbox},{sbox},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    title_end = 0.0
    if title:
        text, sub, title_end = title
        text, sub = clean(text), clean(sub)
        if title_end - offset > 0:
            events.append(f"Dialogue: 1,{ass_time(0)},{ass_time(title_end - offset)},Title,,0,0,0,,"
                          f"{{\\pos(140,{h * 0.36:.0f})\\fad(0,120)}}{text}")
            events.append(f"Dialogue: 1,{ass_time(0)},{ass_time(title_end - offset)},Title,,0,0,0,,"
                          f"{{\\pos(144,{h * 0.36 + st['title_size'] * 1.45:.0f})\\fn{st['font_text_weight']}"
                          f"\\fs{st['title_size'] * 0.42:.0f}\\fad(0,120)}}{sub}")
    for a, b, spk, text in lines:
        a, b = max(a, title_end) - offset, b - offset
        if b <= max(0.0, a):
            continue
        text = clean(text.replace("\\", "").replace("{", "(").replace("}", ")"))
        events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Caption,,0,0,0,,"
                      f"{{\\fn{st['font_name_weight']}\\c{ass_color(colors[spk])}}}{label(spk, people)}: "
                      f"{{\\fn{st['font_text_weight']}\\c{ass_color(st['caption_text'])}}}{text}")
    path.write_text(head + "\n".join(list(events) + list(story)) + "\n")


# ---------------------------------------------------------------- render

class Decoder:
    """Graded BGR frames of one camera, read forward to any (increasing) source time."""

    def __init__(self, ev: dict, cam_id: str, t0: float, dur: float, fps: float, size: tuple, grade: str):
        cam = synced(ev, cam_id)
        self.w, self.h = size
        self.t0, self.fps = t0, fps
        file_t = t0 - cam["start"]
        vf = f"fps={fps}"
        if cam.get("rotate"):  # level a tilted camera; zoom just enough that no corner shows
            a = math.radians(float(cam["rotate"]))
            zoom = math.cos(abs(a)) + max(self.w / self.h, self.h / self.w) * math.sin(abs(a)) + 0.002
            vf += (f",rotate={a:.6f}:ow=iw:oh=ih:c=black,scale={round(self.w * zoom / 2) * 2}:-2,"
                   f"crop={self.w}:{self.h}")
        vf += f",{grade}" if grade else ""
        self.proc = subprocess.Popen(
            [str(Path(ffmpeg_with_libass())), "-nostdin", "-v", "error", "-ss", f"{max(0.0, file_t):.4f}",
             "-i", str(media(ev, cam["file"])), "-t", f"{dur + 2:.3f}", "-vf", vf,
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self.w * self.h * 3 * 2)
        self.n = -1
        self.last = np.zeros((self.h, self.w, 3), np.uint8)

    def at(self, t: float) -> np.ndarray:
        target = int(round((t - self.t0) * self.fps))
        size = self.w * self.h * 3
        while self.n < target:
            buf = self.proc.stdout.read(size)
            if len(buf) < size:
                break
            self.last = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
            self.n += 1
        return self.last

    def close(self):
        self.proc.stdout.close()
        self.proc.wait()


def render(ev: dict, edir: Path, p: dict, start: float, dur: float, plan_only: bool) -> Path:
    st = style()
    out_w, out_h = st["size"]
    fps = output_fps(ev)
    pstart, pend = panel_range(ev, p)
    words_src = json.loads((edir / "transcripts" / f"{p['id']}.json").read_text())["words"]
    story = load_story(edir / "longform" / f"{p['id']}.story.yaml")
    ed = tighten.build(edir, p, words_src, pend - pstart, story["cuts"])
    words = ed.words_out(words_src)
    total = ed.out_dur
    dur = min(dur, total - start) if dur else total - start
    tracks = Tracks(edir / "faces" / f"{p['id']}.cam2.tracks.json")
    people = {x["name"]: x for x in p.get("people", []) if isinstance(x, dict)}
    hosts = {n for n, x in people.items() if re.search(r"host|emcee", x.get("role") or "")}
    seats = list(((p.get("seats") or {}).get("cam2")) or [])
    order = seats + [n for n in people if n not in seats]
    colors = {n: st["name_colors"][i % len(st["name_colors"])] for i, n in enumerate(order)}
    for w in words:
        colors.setdefault(w["speaker"], st["name_colors"][len(colors) % len(st["name_colors"])])
    title_end = st["title_seconds"] if st.get("title_seconds") else 0.0
    themes = sorted(set(round(max(title_end + 0.5, ed.to_out_near(x["t"])), 3)
                        for x in story["questions"] + story["anchors"]))
    shots = plan(words, total, tracks, st, ed.chops() + themes, ed.to_src, hosts)
    shots = split_at(shots, themes)
    if title_end:  # the opening is the title card, then the first shot
        shots = [s for s in shots if s["t1"] > title_end]
        shots[0]["t0"] = 0.0
        shots.insert(0, {"kind": "title", "cam": None, "person": None, "speaker": None, "t0": 0.0, "t1": title_end})
        shots[1]["t0"] = title_end

    out_dir = edir / "longform"
    out_dir.mkdir(exist_ok=True)
    tag = p["id"] if start == 0 and dur >= total - 0.01 else f"{p['id']}.{int(start)}-{int(start + dur)}"
    (out_dir / f"{tag}.plan.json").write_text(json.dumps({"edit": ed.summary(), "shots": shots}, indent=1))
    ass = out_dir / f"{tag}.ass"
    date = ev.get("date")
    sub = ", ".join(x for x in (ev.get("name") or ev.get("event"), date.strftime("%b %-d") if hasattr(date, "strftime") else str(date or "")) if x)
    write_ass(ass, caption_lines(words, people, st), people, colors, st, start,
              (p.get("title") or p["id"], sub, title_end) if title_end else None,
              story_events(story, lambda t: max(title_end + 0.5, ed.to_out_near(t)), people, colors, st, start,
                           start + dur))
    counts = {}
    for s in shots:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    print(f"[{p['id']}] edit: {ed.summary()}")
    print(f"[{p['id']}] {len(shots)} shots ({', '.join(f'{v} {k}' for k, v in counts.items())}), "
          f"median shot {np.median([s['t1'] - s['t0'] for s in shots]):.1f}s, {len(ed.chops())} jump cuts, "
          f"{len(themes)} theme wipes ({len(story['questions'])} questions, {len(story['anchors'])} anchors)")
    if plan_only:
        return out_dir / f"{tag}.plan.json"

    sizes = {}
    for c in ("cam1", "cam2"):
        info = probe(media(ev, synced(ev, c)["file"]))
        sizes[c] = (info["width"], info["height"])
    src0 = ed.to_src(start)
    span = ed.to_src(min(total, start + dur)) - src0
    dec = {c: Decoder(ev, c, pstart + src0, span, fps, sizes[c], st.get("grade") or "") for c in sizes}
    card = title_card(out_w, out_h, st) if title_end else None
    chops = ed.chops()
    wipes = sorted(([title_end] if title_end else []) + themes)
    with tempfile.TemporaryDirectory() as tmp:
        audio = ed.audio()[int(start * tighten.SR):int((start + dur) * tighten.SR)]
        wav = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(tighten.SR), "-ac", "1",
                        "-i", "-", "-c:a", "pcm_f32le", str(wav)], input=audio.astype(np.float32).tobytes(), check=True)
        out = out_dir / f"{tag}.mp4"
        enc = subprocess.Popen(
            [ffmpeg_with_libass(), "-nostdin", "-v", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "-",
             "-i", str(wav),
             "-filter_complex", f"[0:v]subtitles=filename='{str(ass).replace(':', chr(92) + ':')}'[v]",
             "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)],
            stdin=subprocess.PIPE)
        wipe = st["wipe_seconds"]
        frames = int(round(dur * fps))
        si = 0
        began = time.time()
        try:
            for n in range(frames):
                T = start + n / fps
                t_src = ed.to_src(T)
                src = {c: d.at(pstart + t_src) for c, d in dec.items()}
                while si + 1 < len(shots) and shots[si + 1]["t0"] <= T:
                    si += 1
                shot = shots[si]
                nxt = shots[si + 1] if si + 1 < len(shots) else None
                prev = shots[si - 1] if si > 0 else None
                if shot["kind"] == "title":
                    frame = card.copy()
                else:
                    jumps = bisect_right(chops, T) - bisect_right(chops, shot["t0"])
                    rect = shot_rect(shot, T, t_src, (jumps % 2 == 1) != shot.get("flip", False), tracks, sizes, st)
                    frame = warp(src[shot["cam"]], rect, out_w, out_h)
                # a wipe marks a theme change (title end, new question, new anchor); other cuts are hard
                k = bisect_right(wipes, T + wipe / 2) - 1
                if k >= 0 and abs(T - wipes[k]) < wipe / 2:
                    draw_wipe(frame, (T - (wipes[k] - wipe / 2)) / wipe, st["wipe_colors"])
                if st.get("progress_height") and shot["kind"] != "title":
                    draw_progress(frame, T / total, st)
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
    ap.add_argument("--start", type=float, default=0.0, help="output seconds")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds (default: to the end)")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    render(ev, edir, panel(ev, args.panel), args.start, args.duration, args.plan_only)


if __name__ == "__main__":
    main()
