"""Edit a full panel as a 16:9 video in the ShortsHog look.

    .venv/bin/python scripts/longform.py <event> <panel> [--start S] [--duration D] [--plan-only]

The speaker's point is the content: the picture changes when the information changes, not on
a timer. Style and pacing live in FORMAT.md ("## Dynamic edit" and "## Long form").

  1. Tighten (edit.py): ums muted (room tone underneath), long pauses / long ums / stutters
     cut with short crossfades. Everything below runs on that tightened timeline.
  2. Shots, by who's talking (labelled transcript): a close-up on the speaker (cam2,
     following their tracked face) that holds. A cutaway (cam1 wide, cam2 group, or a
     listener's reaction close-up) only on a trigger (`cutaway_triggers`) and only at a
     sentence boundary: after an anchor sentence, as the host starts a new question, on a
     speaker change after 3+ close-ups in a row, or once a shot has held shot_max. A new
     speaker's shot starts l_cut seconds into their first word.
  3. Cuts are hard cuts. The PostHog stripe wipe marks a theme change only: a new
     question or a new anchor from the panel's story file. A jump cut inside a shot
     (a removed pause) punches the framing in or out a little. An anchor sentence stays
     on the speaker in a tighter close-up; only wides and group shots push in.
  4. Story (events/<e>/longform/<panel>.story.yaml, panel time): a chapter tag for the
     current question, top left, and anchor cards that pop up top right with a
     speaker's key point. Clean dark cards; on-screen text never uses middots or dashes.
     `cold_open: {from, to}` (transcript text) plays one anchor sentence before the title.
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
from bisect import bisect_left, bisect_right
from pathlib import Path

import cv2
import numpy as np
from ruamel.yaml import YAML

import edit as tighten
from common import REPO, event_dir, ffmpeg_with_libass, load_event, media, output_fps, panel, panel_range, probe, synced

FACE_Y = 0.40       # face centre sits this far down a close-up
SENTENCE_END = re.compile(r"[.?!][\"')\u201d]*$")


def style() -> dict:
    text = (REPO / "FORMAT.md").read_text()
    m = re.search(r"^## Long form\n.*?```yaml\n(.*?)```", text, re.S | re.M)
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

def turns(words: list, dur: float, min_len: float, l_cut: float = 0.0) -> list:
    """[(speaker, t0, t1)] covering [0, dur]; turns shorter than min_len fold into the previous one.
    A turn's shot starts l_cut seconds into its first word (the previous shot holds over it)."""
    out = []
    for w in words:
        if not out or out[-1][0] != w["speaker"]:
            out.append([w["speaker"], w["start"] + l_cut, w["end"]])
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


def sentence_ends(words: list, st: dict) -> list:
    """For each word, whether a sentence ends after it: it ends in . ? ! (or ended on a word the edit
    cut), the speaker or a short's segment changes next, or, inside a stretch whisper left unpunctuated
    (no punctuation within `unpunctuated_window` words either side), the speaker pauses at least
    `boundary_pause` (source time) or restarts on a capital after `boundary_capital_pause`."""
    n = len(words)
    punct = [bool(re.search(r"[.,?!;:]['\")\u201d]*$", w["w"])) for w in words]
    k = st.get("unpunctuated_window", 6)
    cum = np.concatenate([[0], np.cumsum(punct)])
    out = []
    for i, p in enumerate(words):
        if i + 1 == n:
            out.append(None)
            continue
        q = words[i + 1]
        if SENTENCE_END.search(p["w"]) or p.get("eos"):
            out.append("sentence")
        elif p["speaker"] != q["speaker"] or p.get("seg") != q.get("seg"):
            out.append("turn")
        elif cum[min(n, i + k + 1)] - cum[max(0, i - k + 1)] == 0:
            gap = (q.get("src") or [q["start"]])[0] - (p.get("src") or [0, p["end"]])[1]
            cap = q["w"][:1].isupper() and not re.match(r"I('|$)", q["w"])
            out.append("pause" if gap >= st["boundary_pause"] or (cap and gap >= st["boundary_capital_pause"]) else None)
        else:
            out.append(None)
    return out


def boundaries(words: list, chops: list, st: dict) -> list:
    """Cut times between sentences (see sentence_ends): on removed audio if the gap holds some, else
    just before the next sentence starts."""
    out = []
    for p, n, why in zip(words, words[1:], sentence_ends(words, st)):
        if not why:
            continue
        lo, hi = p["end"], max(p["end"], n["start"])
        inside = chops[bisect_left(chops, lo - 0.01):bisect_right(chops, hi + 0.01)]
        out.append(round(inside[-1] if inside else max(lo, hi - 0.12), 3))
    return sorted(set(out))


def cut_points(words: list, dur: float, st: dict, chops: list) -> tuple:
    """(sentence boundaries, turn starts): every time the picture may change."""
    return boundaries(words, chops, st), [round(a, 3) for _, a, _ in turns(words, dur, st["min_shot"], st["l_cut"])[1:]]


def anchor_spans(story_anchors: list, theme_at, bounds: list, dur: float) -> list:
    """[(A0, A1, speaker)]: each anchor sentence, from its theme time to the sentence boundary after it."""
    out = []
    for an in story_anchors:
        a0 = theme_at(an["t"])
        a1 = bounds[bisect_right(bounds, a0 + 0.3)] if bisect_right(bounds, a0 + 0.3) < len(bounds) else dur
        out.append((a0, min(a1, a0 + 15.0), an["speaker"]))
    return out


def plan(words: list, dur: float, tracks: Tracks, st: dict, chops: list, to_src, hosts: set,
         anchors: list = (), questions: list = ()) -> list:
    """[{kind, cam, person, speaker, t0, t1}] on the edit timeline, covering [0, dur].

    The speaker's shot holds. The picture cuts away only on a trigger in st["cutaway_triggers"],
    and a cutaway only ever starts at a sentence boundary (or a turn start):
      anchor_end      a listener's reaction (react_for) once an anchor sentence finishes
      new_question    a wide or group shot (cutaway_for) as the host starts a new question
      speaker_change  the new speaker's shot l_cut into their first word; a wide first after
                      wide_after_closes close-ups in a row
      shot_max        a shot that has held shot_max cuts away at the next sentence end
    Nothing cuts away inside an anchor sentence: anchors [(A0, A1, speaker)]; questions [t]."""
    trig = set(st["cutaway_triggers"])
    bounds = boundaries(words, chops, st)
    ends = {round(a1, 3) for _, a1, _ in anchors}
    qs = sorted(questions)
    state = {"last": None, "react": None, "q": 0}

    def seen(name, a, b):
        return tracks.seen_share(name, to_src(a), to_src(b))

    def in_anchor(t):
        return any(a0 - 1e-3 < t < a1 - 1e-3 for a0, a1, _ in anchors)

    def closes_in_row(shots):
        n = 0
        for s in reversed(shots):
            if s["kind"] not in ("close", "react"):
                break
            n += 1
        return n

    def cutaway(shots, kind, spk, close_ok, c0, b, prev):
        """A cutaway shot from c0 inside a turn that ends at b, or None if there's no room."""
        hold = st["react_for"] if kind == "react" else st["cutaway_for"]
        c1 = c0 + hold
        if b - c1 < st["min_shot"]:
            c1 = b
        c1 = min([c1] + [a0 for a0, _, _ in anchors if c0 + 1e-3 < a0 < c1])  # the anchor sentence starts on the speaker
        if c1 - c0 < 0.8:
            return None
        who = None
        if kind == "react":
            cands = [n for n in tracks.seats if n != spk and seen(n, c0, c1) >= 0.8]
            pref = [n for n in cands if (n in hosts) != (spk in hosts)] or [n for n in cands if n == prev] or cands
            fresh = [n for n in pref if n != state["react"]] or pref
            who = fresh[0] if fresh else None
            if who is None:
                kind = "wide"
        if not close_ok and kind in ("wide", "react"):  # the speaker is already on the wide
            kind, who = ("group" if shots and shots[-1]["kind"] == "wide" else "wide"), None
        state["last"] = kind
        if who:
            state["react"] = who
        return {"kind": kind, "cam": "cam1" if kind == "wide" else "cam2", "person": who, "speaker": spk,
                "t0": c0, "t1": c1, "cutaway": True}

    shots, prev = [], None
    for spk, a, b in turns(words, dur, st["min_shot"], st["l_cut"]):
        close_ok = (spk is not None and tracks.has(spk) and seen(spk, a, b) >= 0.6
                    and seen(spk, a, min(b, a + 10)) >= 0.75)
        main = {"kind": "close" if close_ok else "wide", "cam": "cam2" if close_ok else "cam1",
                "person": spk if close_ok else None, "speaker": spk}
        t = a
        opener = None
        if a > 0 and not in_anchor(a):
            if "new_question" in trig and any(abs(q - a) < 0.05 for q in qs):
                opener = "group" if state["q"] % 2 else "wide"
                state["q"] += 1
            elif "speaker_change" in trig and closes_in_row(shots) >= st["wide_after_closes"]:
                opener = "wide"
        if opener:
            cut = cutaway(shots, opener, spk, close_ok, a, b, prev)
            if cut:
                shots.append(cut)
                t = cut["t1"]
        start = t
        for c in bounds[bisect_right(bounds, t):bisect_left(bounds, b)]:
            if c < start + st["min_shot"] or b - c < st["min_shot"] or in_anchor(c):
                continue
            kind = None
            if "new_question" in trig and any(abs(q - c) < 0.05 for q in qs):
                kind = "group" if state["q"] % 2 else "wide"
                state["q"] += 1
            elif "anchor_end" in trig and round(c, 3) in ends:
                kind = "react"
            elif "shot_max" in trig and c - start >= st["shot_max"]:
                kind = next(k for k in ("react", "wide", "group") if k != state["last"])
            if not kind:
                continue
            cut = cutaway(shots, kind, spk, close_ok, c, b, prev)
            if not cut:
                continue
            shots.append({**main, "t0": start, "t1": c})
            shots.append(cut)
            start = cut["t1"]
        if b - start > 1e-3:
            shots.append({**main, "t0": start, "t1": b})
        prev = spk
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
        return {"questions": [], "anchors": [], "cuts": [], "wide_only": [], "cold_open": None}
    d = YAML(typ="safe").load(path.read_text()) or {}
    return {"questions": d.get("questions") or [], "anchors": d.get("anchors") or [], "cuts": d.get("cuts") or [],
            "wide_only": d.get("wide_only") or [], "cold_open": d.get("cold_open")}


def wide_only(shots: list, spans: list, to_out) -> list:
    """Hold the wide (cam1) through `spans` (panel time), e.g. while people walk past cam2."""
    out = [(to_out(x["from"]), to_out(x["to"])) for x in spans]
    for s in shots:
        if s["cam"] == "cam2" and any(a < s["t1"] and s["t0"] < b for a, b in out):
            s.update(kind="wide", cam="cam1", person=None)
    merged = []
    for s in shots:
        if merged and (merged[-1]["kind"], merged[-1]["cam"]) == (s["kind"], s["cam"]) == ("wide", "cam1"):
            merged[-1]["t1"] = s["t1"]
        else:
            merged.append(s)
    return merged


def split_at(shots: list, times: list) -> list:
    """Make sure a shot starts at each theme time [(t, flip)], so its wipe hides a cut. With flip, the
    framing punches in or out there; an anchor doesn't flip (its tighter close-up is the change)."""
    for w, flip in sorted(times):
        for i, s in enumerate(shots):
            if s["t0"] < w < s["t1"]:
                if w - s["t0"] >= 0.4 and s["t1"] - w >= 0.4:
                    second = {**s, "t0": w, "flip": s.get("flip", False) != flip}
                    s["t1"] = w
                    shots.insert(i + 1, second)
                break
    return shots


# ---------------------------------------------------------------- framing and graphics

def full_rect(w: int, h: int, zoom: float) -> tuple:
    cw, ch = w / zoom, h / zoom
    return ((w - cw) / 2, (h - ch) / 2, cw, ch)


def shot_rect(shot: dict, T: float, t_src: float, punched: bool, tracks: Tracks, sizes: dict, st: dict,
              anchored: bool = False) -> tuple:
    """Crop on the shot's camera. Close-ups hold still (tighter through an anchor sentence, never past
    max_zoom); only wides and group shots push in, and not during an anchor sentence."""
    w, h = sizes[shot["cam"]]
    if shot["kind"] in ("close", "react"):
        cx, cy, fh = tracks.at(shot["person"], t_src)
        mult = st["anchor_face_mult"] if anchored and shot["kind"] == "close" else st["close_face_mult"]
        ch = min(h, max(h / st["max_zoom"], fh * mult))
        if punched:  # jump cut inside a close-up: step out a little (never tighter than max_zoom)
            ch = min(h, ch * st["punch"])
        cw = ch * 16 / 9
        if cw > w:
            cw, ch = w, w * 9 / 16
        x0 = min(max(0.0, cx - cw / 2), w - cw)
        y0 = min(max(0.0, cy - FACE_Y * ch), h - ch)
        return (x0, y0, cw, ch)
    u = (T - shot["t0"]) / max(0.1, shot["t1"] - shot["t0"])
    zoom = (1 + (0.0 if anchored else st["push_in"] * u)) * (st["punch"] if punched else 1.0)
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
            if w["speaker"] != cur["speaker"] or gap > 0.8 or long or ends or w.get("cold") != cur.get("cold"):
                lines.append(cur)
                cur = None
        if cur is None:
            cur = {"speaker": w["speaker"], "start": w["start"], "end": w["end"], "text": w["w"], "cold": w.get("cold")}
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
    text = re.sub(r"(^|\s)[.,;:!?]+(?=\s|$)", "", text)  # a lone punctuation "word" left by a correction
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
    t_in = t_out = -1.0
    if title:  # (text, subtitle, start, end) in output time
        text, sub, t_in, t_out = title
        text, sub = clean(text), clean(sub)
        if t_out - offset > 0:
            a, b = ass_time(max(0.0, t_in - offset)), ass_time(t_out - offset)
            events.append(f"Dialogue: 1,{a},{b},Title,,0,0,0,,"
                          f"{{\\pos(140,{h * 0.36:.0f})\\fad(0,120)}}{text}")
            events.append(f"Dialogue: 1,{a},{b},Title,,0,0,0,,"
                          f"{{\\pos(144,{h * 0.36 + st['title_size'] * 1.45:.0f})\\fn{st['font_text_weight']}"
                          f"\\fs{st['title_size'] * 0.42:.0f}\\fad(0,120)}}{sub}")
    for a, b, spk, text in lines:
        if b <= t_in + 1e-3:  # before the title card (the cold open)
            b = min(b, t_in)
        else:
            a = max(a, t_out)
        a, b = a - offset, b - offset
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


def cold_open(spec: dict, words_src: list, words: list, ed, total: float) -> tuple | None:
    """(T0, T1, speaker) on the edit timeline for story `cold_open: {from, to}`: from the first word
    (a hard cut in) to just after the last, never into the next word."""
    span = tighten.resolve_cuts(words_src, [spec])
    if not span:
        print(f"  cold open not found: {spec.get('from')!r} .. {spec.get('to')!r}")
        return None
    a, b = ed.to_out_near(span[0][0]), ed.to_out_near(span[0][1])
    inside = [w for w in words if a - 0.01 <= w["start"] < b]
    if not inside:
        return None
    nxt = [w["start"] for w in words if w["start"] >= inside[-1]["end"] - 0.01 and w not in inside]
    t1 = min(inside[-1]["end"] + 0.3, (nxt[0] - 0.05) if nxt else total)
    names = [w["speaker"] for w in inside]
    return inside[0]["start"], max(t1, inside[-1]["end"]), max(set(names), key=names.count)


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
    tracks = Tracks(edir / "faces" / f"{p['id']}.cam2.tracks.json")
    people = {x["name"]: x for x in p.get("people", []) if isinstance(x, dict)}
    hosts = {n for n, x in people.items() if re.search(r"host|emcee", x.get("role") or "")}
    seats = list(((p.get("seats") or {}).get("cam2")) or [])
    order = seats + [n for n in people if n not in seats]
    colors = {n: st["name_colors"][i % len(st["name_colors"])] for i, n in enumerate(order)}
    for w in words:
        colors.setdefault(w["speaker"], st["name_colors"][len(colors) % len(st["name_colors"])])
    title_end = st["title_seconds"] if st.get("title_seconds") else 0.0
    chops = ed.chops()
    bounds, starts = cut_points(words, total, st, chops)
    cands = sorted(set(bounds + starts))

    def theme_at(t):
        """Edit time of a story beat (panel time): the nearest sentence boundary or turn start within 2.5 s;
        a turn start when there's one within a second of it, so the wipe lands on the cut to the new speaker."""
        T = max(0.5, ed.to_out_near(t))
        i = bisect_left(cands, T)
        near = [c for c in cands[max(0, i - 3):i + 3] if abs(c - T) <= 2.5 and c >= 0.5]
        if not near:
            return round(T, 3)
        best = min(near, key=lambda c: abs(c - T))
        turn = [c for c in starts if abs(c - best) <= 1.0 and c >= 0.5]
        return min(turn, key=lambda c: abs(c - T)) if turn else best

    qtimes = [theme_at(q["t"]) for q in story["questions"]]
    anchors = anchor_spans(story["anchors"], theme_at, bounds, total)
    themes = sorted(set(qtimes + [a0 for a0, _, _ in anchors]))
    shots = plan(words, total, tracks, st, chops, ed.to_src, hosts, anchors, qtimes)
    shots = wide_only(shots, story["wide_only"], ed.to_out_near)
    shots = split_at(shots, [(q, True) for q in qtimes if q > 1.0] +
                     [(a0, False) for a0, _, _ in anchors if a0 not in qtimes])

    # output: the cold open (one anchor sentence), the title card on room tone, then the whole panel in order
    cold = cold_open(story["cold_open"], words_src, words, ed, total) if story["cold_open"] else None
    L = 0.0
    if cold:
        c0, c1, cspk = cold
        L = c1 - c0
        if not st["cold_open_min"] <= L <= st["cold_open_max"]:
            print(f"[{p['id']}] WARNING: cold open is {L:.1f}s (want {st['cold_open_min']}-{st['cold_open_max']}s)")
        ok = tracks.has(cspk) and tracks.seen_share(cspk, ed.to_src(c0), ed.to_src(c1)) >= 0.6
        cold_shots = [{"kind": "close" if ok else "wide", "cam": "cam2" if ok else "cam1", "person": cspk if ok else None,
                       "speaker": cspk, "t0": 0.0, "t1": L, "cold": True, "anchor": True}]
    else:
        c0 = c1 = 0.0
        cold_shots = []
    P = L + title_end  # where the panel starts in the output
    title = [{"kind": "title", "cam": None, "person": None, "speaker": None, "t0": L, "t1": P}] if title_end else []
    out_shots = cold_shots + title + [{**s, "t0": round(s["t0"] + P, 3), "t1": round(s["t1"] + P, 3)} for s in shots]
    out_total = total + P
    out_chops = [c - c0 for c in chops if c0 + 0.05 < c < c1 - 0.05] + [c + P for c in chops]
    out_anchors = [(a0 + P, a1 + P, spk) for a0, a1, spk in anchors]
    out_words = ([{**w, "start": w["start"] - c0, "end": min(w["end"], c1) - c0, "cold": True}
                  for w in words if c0 - 0.01 <= w["start"] < c1] if cold else []) + \
                [{**w, "start": w["start"] + P, "end": w["end"] + P} for w in words]
    if out_words and out_words[0].get("cold"):  # the cold open starts mid-thought: capitalise its first word
        out_words[0] = {**out_words[0], "w": out_words[0]["w"][:1].upper() + out_words[0]["w"][1:]}
    dur = min(dur, out_total - start) if dur else out_total - start

    out_dir = edir / "longform"
    out_dir.mkdir(exist_ok=True)
    tag = p["id"] if start == 0 and dur >= out_total - 0.01 else f"{p['id']}.{int(start)}-{int(start + dur)}"
    (out_dir / f"{tag}.plan.json").write_text(json.dumps(
        {"edit": ed.summary(), "cold_open_seconds": round(L, 3), "panel_starts": round(P, 3),
         "cold_open": {"t0": round(c0, 3), "t1": round(c1, 3), "speaker": cold[2]} if cold else None,
         "anchors": [[round(a, 3), round(b, 3), n] for a, b, n in out_anchors], "shots": out_shots}, indent=1))
    ass = out_dir / f"{tag}.ass"
    date = ev.get("date")
    sub = ", ".join(x for x in (ev.get("name") or ev.get("event"), date.strftime("%b %-d") if hasattr(date, "strftime") else str(date or "")) if x)
    write_ass(ass, caption_lines(out_words, people, st), people, colors, st, start,
              (p.get("title") or p["id"], sub, L, P) if title_end else None,
              story_events(story, lambda t: theme_at(t) + P, people, colors, st, start, start + dur))
    counts = {}
    for s in out_shots:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    real = [s["t1"] - s["t0"] for s in out_shots if s["kind"] != "title"]
    print(f"[{p['id']}] edit: {ed.summary()}")
    if cold:
        print(f"[{p['id']}] cold open {L:.1f}s ({cold[2]}): " + " ".join(w["w"] for w in out_words if w.get("cold")))
    print(f"[{p['id']}] {len(out_shots)} shots ({', '.join(f'{v} {k}' for k, v in counts.items())}), "
          f"average {np.mean(real):.1f}s, median {np.median(real):.1f}s, {len(chops)} jump cuts, "
          f"{len(themes)} theme wipes ({len(story['questions'])} questions, {len(story['anchors'])} anchors)")
    if plan_only:
        return out_dir / f"{tag}.plan.json"

    sizes = {}
    for c in ("cam1", "cam2"):
        info = probe(media(ev, synced(ev, c)["file"]))
        sizes[c] = (info["width"], info["height"])
    card = title_card(out_w, out_h, st) if title_end else None
    wipes = sorted(([P] if title_end else []) + [t + P for t in themes if t > 1.0])
    shot_starts = [s["t0"] for s in out_shots]
    with tempfile.TemporaryDirectory() as tmp:
        full = ed.audio()
        SR = tighten.SR
        n_title = int(title_end * SR)
        tone = tighten.loop(ed.room_tone(), n_title, int(tighten.XFADE * SR)) if n_title else full[:0]
        both = np.concatenate([full, tone])
        audio = tighten.splice(both, ([(int(c0 * SR), int(c1 * SR))] if cold else []) +
                               ([(len(full), len(both))] if n_title else []) + [(0, len(full))])
        audio = audio[int(start * SR):int((start + dur) * SR)].copy()
        k = int(0.005 * SR)  # no click on the very first sample
        audio[:k] *= np.linspace(0, 1, k, dtype=np.float32)
        wav = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1",
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
        dec, dec_part = {}, None
        began = time.time()
        try:
            for n in range(frames):
                O = start + n / fps
                part = "cold" if O < L else "title" if O < P else "panel"
                T = c0 + O if part == "cold" else max(0.0, O - P)
                t_src = ed.to_src(T)
                if part != dec_part and part != "title":  # each part reads its own stretch of the cameras
                    for d in dec.values():
                        d.close()
                    end_src = ed.to_src(c1) if part == "cold" else ed.to_src(min(total, start + dur - P))
                    dec = {c: Decoder(ev, c, pstart + t_src, max(0.1, end_src - t_src), fps, sizes[c],
                                      st.get("grade") or "") for c in sizes}
                    dec_part = part
                src = {c: d.at(pstart + t_src) for c, d in dec.items()} if part != "title" else {}
                si = max(0, bisect_right(shot_starts, O) - 1)
                shot = out_shots[si]
                if shot["kind"] == "title":
                    frame = card.copy()
                else:
                    jumps = bisect_right(out_chops, O) - bisect_right(out_chops, shot["t0"])
                    anchored = shot.get("anchor") or any(a <= O < b and shot["person"] in (spk, None)
                                                         for a, b, spk in out_anchors)
                    rect = shot_rect(shot, O, t_src, (jumps % 2 == 1) != shot.get("flip", False), tracks, sizes, st,
                                     anchored)
                    frame = warp(src[shot["cam"]], rect, out_w, out_h)
                # a wipe marks a theme change (title end, new question, new anchor); other cuts are hard
                k = bisect_right(wipes, O + wipe / 2) - 1
                if k >= 0 and abs(O - wipes[k]) < wipe / 2:
                    draw_wipe(frame, (O - (wipes[k] - wipe / 2)) / wipe, st["wipe_colors"])
                if st.get("progress_height") and shot["kind"] != "title":
                    draw_progress(frame, O / out_total, st)
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
