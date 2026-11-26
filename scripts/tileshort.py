"""Cut 9:16 shorts of one person's tile from a video-call grid recording, in the ShortsHog look.

    .venv/bin/python scripts/tileshort.py <event> <panel> [--id SHORT_ID ...] [--plan-only] [--stills]

For webinar / call recordings where every panelist is a fixed tile in one frame (no cam2,
no seats). Specs live under `tile_shorts:` in events/<event>/longform/<panel>.story.yaml:
    {id, speaker, headline, key, color, title?, from, to | parts: [{from, to}], hook?, skip?}
headline, key, color and title are on-screen text; the rest is transcript text. Style: "## Tile short" in FORMAT.md.

  - Tightened like the 3:4 shorts ("## Short-form edit": every um cut, pauses down to a breath,
    restarts cut, a second whisper pass for the ums the first one missed), 15 ms crossfades.
  - Camera: only the speaker's tile (event.yaml `speakers.<name>.tile`): the widest 9:16 crop of it
    (the recording is low-res), the face centred (YuNet, smoothed), a slow drift and a slight push-in.
    The call app's own name tag (`name_tag`, camera pixels) is painted out of the footage.
  - On screen: the headline on a frosted box at the top from frame 1 (heavier RoundHog, the `key` phrase on
    a highlighter bar in the short's PostHog colour); a lower-third name strap just under the captions
    for the first strap_seconds; captions with no box (white RoundHog, black outline, the word being said
    in the caption highlight colour), always below the mouth; the tangerine progress bar.
Output: events/<event>/tileshort/<id>.mp4 (+ .ass, .plan.json).
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from ruamel.yaml import YAML

import edit as tighten
import longform as lf
import shortform as sf
from common import REPO, event_dir, ffmpeg_with_libass, load_event, media, output_fps, panel, panel_range, synced

FONT_DIR = Path.home() / "Library" / "Fonts"
FACE_MODEL = REPO / "models" / "face_detection_yunet_2023mar.onnx"


def style() -> dict:
    text = (REPO / "FORMAT.md").read_text()
    m = re.search(r"^## Tile short\n.*?```yaml\n(.*?)```", text, re.S | re.M)
    if not m:
        sys.exit("FORMAT.md needs a ```yaml block under '## Tile short'")
    return {**lf.style(), **YAML(typ="safe").load(m.group(1))}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    name = {"Bold": "RoundHog-Bold.ttf", "SemiBold": "RoundHog-SemiBold.ttf", "ExtraBold": "RoundHog-ExtraBold.ttf",
            "Black": "RoundHog-Black.ttf"}.get(weight, "RoundHog.ttf")
    return ImageFont.truetype(str(FONT_DIR / name), size)


def ease_out(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 1 - (1 - u) ** 3


def ease_io(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 3 * u * u - 2 * u * u * u


# ---------------------------------------------------------------- graphics (drawn once, composited per frame)

class Layer:
    """An RGBA image placed at (x, y) on the frame, composited onto BGR frames."""

    def __init__(self, img: Image.Image, x: int, y: int):
        a = np.asarray(img).astype(np.float32)
        self.bgr = a[..., [2, 1, 0]]
        self.alpha = a[..., 3:4] / 255.0
        self.x, self.y = x, y
        self.h, self.w = a.shape[:2]

    def draw(self, frame: np.ndarray, opacity: float = 1.0) -> None:
        if opacity <= 0:
            return
        roi = frame[self.y:self.y + self.h, self.x:self.x + self.w].astype(np.float32)
        al = self.alpha * opacity
        frame[self.y:self.y + self.h, self.x:self.x + self.w] = (roi * (1 - al) + self.bgr * al).astype(np.uint8)


def rgb_of(hex_rgb: str) -> tuple:
    return tuple(int(hex_rgb[k:k + 2], 16) for k in (1, 3, 5))


def ink_on(hex_rgb: str) -> tuple:
    """Dark text on a light brand colour, white on a dark one."""
    r, g, b = (c / 255 for c in rgb_of(hex_rgb))
    return (21, 21, 21, 255) if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.45 else (255, 255, 255, 255)


class Title:
    """The headline on a frosted box at the top, on screen from frame 1: the footage behind it blurred and
    tinted (live, every frame), the text in a heavier RoundHog than the captions, left-aligned, the key
    phrase on a highlighter bar in the short's colour. It reads as a title, not a subtitle."""

    def __init__(self, spec: dict, st: dict, color: str):
        w = st["size"][0]
        m, pad = st["title_margin"], st["title_pad"]
        f = font(st["title_weight"], st["title_size"])
        words = lf.clean(spec["headline"]).split()
        kw = lf.clean(spec.get("key") or "").split()
        nrm = lambda x: re.sub(r"\W", "", x).lower()
        lo = next((i for i in range(len(words)) if [nrm(x) for x in words[i:i + len(kw)]] == [nrm(x) for x in kw]),
                  None) if kw else None
        if kw and lo is None:
            print(f"[{spec['id']}] WARNING: key {' '.join(kw)!r} isn't in the headline")
        keyed = set(range(lo, lo + len(kw))) if lo is not None else set()
        maxw = w - 2 * m - 2 * pad
        space = f.getlength(" ")
        lines, cur, cur_w = [], [], 0.0
        for i, word in enumerate(words):
            ww = f.getlength(word)
            if cur and cur_w + space + ww > maxw:
                lines.append(cur)
                cur, cur_w = [], 0.0
            cur.append((i, word, ww))
            cur_w += (space if len(cur) > 1 else 0) + ww
        lines.append(cur)
        lh = int(st["title_size"] * st["title_leading"])
        self.box = (m, st["title_top"], w - m, st["title_top"] + 2 * pad + lh * (len(lines) - 1) + st["title_size"] + 12)
        x0, y0, x1, y1 = self.box
        img = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, x1 - x0 - 1, y1 - y0 - 1], radius=st["title_radius"],
                            outline=(255, 255, 255, int(255 * st["title_border_alpha"])), width=2)
        hp, hl = st["title_highlight_pad"], rgb_of(color) + (255,)
        for n, line in enumerate(lines):  # the highlighter bars first, one per run of key words on a line
            x, y = pad, pad + n * lh
            run = None
            for i, word, ww in line:
                if i in keyed:
                    run = [x, x + ww] if run is None else [run[0], x + ww]
                x += ww + space
            if run:
                d.rounded_rectangle([run[0] - hp, y - 2, run[1] + hp, y + st["title_size"] + 12],
                                    radius=st["title_highlight_radius"], fill=hl)
        for n, line in enumerate(lines):
            x, y = pad, pad + n * lh
            for i, word, ww in line:
                d.text((x, y), word, font=f, fill=ink_on(color) if i in keyed else (255, 255, 255, 255))
                x += ww + space
        self.text = Layer(img, x0, y0)
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, x1 - x0 - 1, y1 - y0 - 1], radius=st["title_radius"], fill=255)
        self.mask = (np.asarray(mask, np.float32) / 255)[..., None]
        self.tint = np.array(lf.bgr(st["title_tint"]), np.float32)
        self.tint_a, self.blur = st["title_tint_alpha"], st["title_blur"]

    def draw(self, frame: np.ndarray) -> None:
        x0, y0, x1, y1 = self.box
        roi = frame[y0:y1, x0:x1]
        small = cv2.resize(roi, (max(1, (x1 - x0) // 4), max(1, (y1 - y0) // 4)), interpolation=cv2.INTER_AREA)
        frost = cv2.resize(cv2.GaussianBlur(small, (0, 0), self.blur / 4), (x1 - x0, y1 - y0),
                           interpolation=cv2.INTER_LINEAR).astype(np.float32)
        frost = frost * (1 - self.tint_a) + self.tint * self.tint_a
        frame[y0:y1, x0:x1] = (roi * (1 - self.mask) + frost * self.mask).astype(np.uint8)
        self.text.draw(frame)


def name_strap(name: str, title: str, st: dict, color: str) -> Layer:
    """Lower third: the name on a dark pill, the role on a tab in the short's colour under it, left edges
    aligned. Placed by the caller (centred, just under the captions)."""
    fn, fr = font("Bold", st["strap_name_size"]), font("SemiBold", st["strap_role_size"])
    px, py = st["strap_pad_x"], st["strap_pad_y"]
    nw, rw = int(fn.getlength(name) + 2 * px), int(fr.getlength(title) + 2 * px) if title else 0
    nh, rh = st["strap_name_size"] + 2 * py, (st["strap_role_size"] + 2 * py - 4) if title else 0
    w, h = max(nw, rw), nh + (rh if title else 0)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = st["strap_radius"]
    d.rounded_rectangle([0, 0, nw - 1, nh - 1], radius=r, fill=rgb_of(st["strap_bg"]) + (int(255 * st["strap_bg_alpha"]),))
    d.text((px, py - 3), name, font=fn, fill=(255, 255, 255, 255))
    if title:
        d.rounded_rectangle([0, nh, rw - 1, nh + rh - 1], radius=r, fill=rgb_of(color) + (255,))
        d.text((px, nh + py - 4), title, font=fr, fill=ink_on(color))
    return Layer(img, 0, 0)


# ---------------------------------------------------------------- footage

class TileDecoder:
    """Graded BGR frames of one tile of a camera, read forward to any (increasing) panel time."""

    def __init__(self, ev: dict, cam_id: str, tile: tuple, t0: float, dur: float, fps: float, grade: str):
        cam = synced(ev, cam_id)
        x, y, self.w, self.h = tile
        self.t0, self.fps = t0, fps
        vf = f"fps={fps},crop={self.w}:{self.h}:{x}:{y}" + (f",{grade}" if grade else "")
        self.proc = subprocess.Popen(
            [ffmpeg_with_libass(), "-nostdin", "-v", "error", "-ss", f"{max(0.0, t0 - cam['start']):.4f}",
             "-i", str(media(ev, cam["file"])), "-t", f"{dur + 1:.3f}", "-vf", vf,
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


def face_track(ev: dict, cam_id: str, tile: tuple, spans: list, pstart: float, tag: str) -> tuple:
    """(times, cx, cy, h) of the biggest face in the tile, panel time, sampled at 4 fps over each source
    span and smoothed (gaussian, ~0.8 s), so the crop follows the speaker without shaking."""
    x, y, w, h = tile
    det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), 0.6, 0.3, 50)
    ts, pts = [], []
    for a, b in spans:
        dec = TileDecoder(ev, cam_id, tile, pstart + a, b - a, 4.0, "")
        for t in np.arange(a, b, 0.25):
            img = dec.at(pstart + t)
            _, faces = det.detect(img)
            if faces is not None and len(faces):
                f = max(faces, key=lambda r: r[2] * r[3])
                ts.append(t)
                pts.append((f[0] + f[2] / 2, f[1] + f[3] / 2, f[3]))
        dec.close()
    if not ts:
        print(f"[{tag}] WARNING: no face found in the tile; holding the centre")
        return np.array([0.0]), np.array([w / 2]), np.array([h * 0.45]), np.array([h * 0.3])
    ts, pts = np.array(ts), np.array(pts)
    k = np.exp(-0.5 * (np.arange(-6, 7) / 3.2) ** 2)
    k /= k.sum()
    sm = [np.convolve(np.pad(pts[:, i], 6, mode="edge"), k, mode="valid") for i in range(3)]
    print(f"[{tag}] face found in {len(ts)} of {int(sum((b - a) * 4 for a, b in spans))} samples")
    return ts, sm[0], sm[1], sm[2]


# ---------------------------------------------------------------- captions

def caption_line_groups(words: list, st: dict) -> list:
    """Words grouped into caption lines: a pause, a full line, or punctuation ends one."""
    lines, cur = [], None
    for x in words:
        if cur is not None:
            text = " ".join(y["w"] for y in cur)
            if (x["seg"] != cur[-1]["seg"] or x["start"] - cur[-1]["end"] > 0.5
                    or len(text) + 1 + len(x["w"]) > st["caption_max_chars"]
                    or re.search(r"[.?!,]$", text) and len(text) > 8):
                lines.append(cur)
                cur = None
        cur = [x] if cur is None else cur + [x]
    return lines + ([cur] if cur else [])


def write_ass(path: Path, lines: list, st: dict, dur: float, hi: str, margins: list) -> None:
    """Captions with no box: white RoundHog with a black outline, the word being said in `hi` (a PostHog
    colour). One event per word, so exactly one word is lit at a time. margins[k]: line k's MarginV."""
    w, h = st["size"]
    white, black = lf.ass_color("#FFFFFF"), lf.ass_color("#000000")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{st['caption_font']},{st['caption_size']},{white},{white},{black},{black},0,0,0,0,100,100,0,0,1,{st['caption_outline']},{st['caption_shadow']},2,80,80,0,1
"""
    ev = []
    clean = lambda t: lf.clean(t.replace("\\", "").replace("{", "(").replace("}", ")"))
    for k, ln in enumerate(lines):
        end = max(ln[-1]["end"] + 0.25, ln[0]["start"] + 0.35)
        if k + 1 < len(lines):
            end = min(end, lines[k + 1][0]["start"])
        end = min(end, dur)
        texts = [clean(x["w"]) for x in ln]
        for i, x in enumerate(ln):
            a = ln[0]["start"] if i == 0 else x["start"]
            b = ln[i + 1]["start"] if i + 1 < len(ln) else end
            if b - a < 0.01 or a >= dur:
                continue
            body = " ".join(f"{{\\c{lf.ass_color(hi)}}}{t}{{\\c{white}}}" if j == i else t for j, t in enumerate(texts))
            ev.append(f"Dialogue: 0,{lf.ass_time(max(0.0, a))},{lf.ass_time(b)},Caption,,0,0,{margins[k]},,{body}")
    path.write_text(head + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                    + "\n".join(ev) + "\n")


# ---------------------------------------------------------------- render

def render(ev: dict, edir: Path, p: dict, spec: dict, plan_only: bool, stills: bool) -> Path | None:
    st = style()
    out_w, out_h = st["size"]
    fps = output_fps(ev)
    pstart, pend = panel_range(ev, p)
    tag = spec["id"]
    speaker = spec["speaker"]
    who = (ev.get("speakers") or {}).get(speaker) or {}
    if not who.get("tile"):
        sys.exit(f"[{tag}] event.yaml speakers.{speaker} needs `tile: [x, y, w, h]`")
    ins = st["tile_inset"]
    tx, ty, tw, th = who["tile"]
    tile = (tx + ins, ty + ins, tw - 2 * ins, th - 2 * ins)
    tile = tuple(int(round(v / 2) * 2) for v in tile)
    words_src = json.loads((edir / "transcripts" / f"{p['id']}.json").read_text())["words"]
    story = lf.load_story(edir / "longform" / f"{p['id']}.story.yaml")

    # --- the edit: the same tightening and segment logic as the 3:4 shorts
    parts = spec.get("parts") or [{"from": spec["from"], "to": spec["to"]}]
    ranges = []
    for part in parts:
        span = tighten.resolve_cuts(words_src, [part])
        if not span:
            print(f"[{tag}] SKIPPED: couldn't find the range {part['from']!r} .. {part['to']!r}")
            return None
        ranges.append((span[0][0], span[0][1]))
    a, b = min(r[0] for r in ranges), max(r[1] for r in ranges)
    ha = hb = None
    if spec.get("hook"):
        hooks = sf.find_in(words_src, spec["hook"], 0, pend - pstart)
        if not hooks:
            print(f"[{tag}] SKIPPED: couldn't find the hook {spec['hook']!r}")
            return None
        ha, hb = min(hooks, key=lambda x: 0 if a <= x[0] <= b else min(abs(x[0] - a), abs(x[0] - b)))
    skips = [(x, y, "skip", "") for phrase in spec.get("skip") or [] for lo, hi in ranges
             for x, y in sf.find_in(words_src, phrase, lo, hi)]
    cfg = tighten.settings("Short-form edit")
    if cfg.get("filler_scan"):
        skips += sf.scan_fillers(edir, p["id"], ranges + ([(ha, hb)] if ha is not None else []), cfg, tag, words_src)
    editorial = tighten.resolve_cuts(words_src, story["cuts"]) + skips
    ed = tighten.Edit(words_src, tighten.decode_audio(edir / "transcripts" / f"{p['id']}.wav", tighten.SR),
                      pend - pstart, cfg, editorial, tighten.load_bed(edir, p["id"]))
    segs = []
    if ha is not None:
        h0, h1 = sf.padded(ed, words_src, ha, hb, st, pad_start=0.0)
        segs.append((h0, h1, "hook"))
    for pa, pb in ranges:
        t0, t1 = sf.padded(ed, words_src, pa, pb, st, pad_start=0.0 if not segs else None)
        if ha is not None and pa <= ha and hb <= pb and not spec.get("repeat_hook"):
            segs += [(x, y, "meat") for x, y in ((t0, ed.to_out_near(ha) - 0.02), (ed.to_out_near(hb) + 0.04, t1)) if y - x > 0.4]
        else:
            segs.append((t0, t1, "meat"))
    tl = sf.Timeline(segs, ed)
    dur = tl.dur
    words = tl.words(ed.words_out(words_src))
    for i, w in enumerate(words):  # a part can start mid-sentence: capitalise a caption that starts one
        if i == 0 or (words[i - 1]["seg"] != w["seg"] and (lf.SENTENCE_END.search(words[i - 1]["w"]) or words[i - 1].get("eos"))):
            words[i] = {**w, "w": w["w"][:1].upper() + w["w"][1:]}
    others = sorted({w["speaker"] for w in words if w["speaker"] != speaker})
    if others:
        print(f"[{tag}] WARNING: other voices inside the short: {', '.join(others)}")

    def phrase_S(phrase):
        for lo, hi in ranges + ([(ha, hb)] if ha is not None else []):
            for x, y in sf.find_in(words_src, phrase, lo, hi):
                A = ed.to_out(x)
                S = None if A is None else tl.to_S(A)
                if S is not None:
                    return S
        print(f"[{tag}] not found in the short: {phrase!r}")
        return None

    def phrase_span(phrase):  # (S0, S1) of a transcript phrase in the short
        for lo, hi in ranges + ([(ha, hb)] if ha is not None else []):
            for x, y in sf.find_in(words_src, phrase, lo, hi):
                A, B = ed.to_out(x), ed.to_out_near(y)
                S = None if A is None else tl.to_S(A)
                if S is not None:  # a key line that ends the short runs to its end
                    return S, tl.to_S(B) or dur
        print(f"[{tag}] not found in the short: {phrase!r}")
        return None

    # --- graphics: the title (frosted box, from frame 1) and the name strap (a lower third by the captions)
    color = st["colors"][spec["color"]] if spec["color"] in st["colors"] else spec["color"]
    hi_name = (ev.get("caption_highlight") or st["caption_highlight"])
    hi = st["colors"].get(hi_name, hi_name)
    person = next((x for x in p.get("people", []) if isinstance(x, dict) and x["name"] == speaker), {})
    title = spec.get("title") or (f"{person.get('role')} at {person.get('company')}" if person else "")
    title_box = Title(spec, st, color)
    strap = name_strap(lf.clean(speaker), lf.clean(title), st, color)
    strap.x, strap.y = (out_w - strap.w) // 2, out_h - st["strap_bottom"] - strap.h
    s_in, s_out = st["strap_in"], st["strap_in"] + st["strap_seconds"]

    # the call app's own name tag (camera pixels in event.yaml), painted out of the tile
    tags = who.get("name_tag") or []
    tags = [tags] if tags and not isinstance(tags[0], (list, tuple)) else tags
    tag_mask = np.zeros((tile[3], tile[2]), np.uint8)
    for nt in tags:
        x_, y_ = nt[0] - tile[0], nt[1] - tile[1]
        cv2.rectangle(tag_mask, (max(0, x_ - 3), max(0, y_ - 3)), (x_ + nt[2] + 3, y_ + nt[3] + 3), 255, -1)
    mask_box = cv2.boundingRect(tag_mask) if tags else None

    # --- framing: the widest 9:16 crop of the tile, the face centred, a slow drift and push-in
    spans = [(ed.to_src(sg[0]), ed.to_src(sg[1] - 1e-3)) for sg in segs]
    ft, fx, fy, fh = face_track(ev, who.get("camera", "cam1"), tile, spans, pstart, tag)
    _, _, TW, TH = tile
    base_h = min(TH, TW * 16 / 9)
    phase = (sum(map(ord, tag)) % 628) / 100

    # when the speaker sits low in their webcam the widest crop can't centre them: zoom in just enough
    # (at most centre_zoom_max), slowly, so the face still lands mid-frame
    grid = np.arange(0, dur + 0.1, 0.1)
    need = []
    for S in grid:
        t_src = tl.to_src(min(S, dur - 1e-3))
        room = TH - float(np.interp(t_src, ft, fy))  # face centre to the tile's bottom
        need.append(min(st["centre_zoom_max"], max(1.0, base_h * (1 - st["face_y"]) / max(room, 1.0))))
    need = np.array(need)
    k = max(1, int(st["centre_zoom_smooth"] * 10))
    need = np.array([need[max(0, i - k):i + k + 1].max() for i in range(len(need))])  # reach it before it's needed
    g = np.exp(-0.5 * (np.arange(-2 * k, 2 * k + 1) / k) ** 2)
    need = np.convolve(np.pad(need, 2 * k, mode="edge"), g / g.sum(), mode="valid")

    def crop(S, t_src):
        u = S / max(dur, 1e-6)
        ch = base_h / max(1 + st["push_in"] * ease_io(u), float(np.interp(S, grid, need)))
        cw = ch * 9 / 16
        cx = float(np.interp(t_src, ft, fx))
        cy = float(np.interp(t_src, ft, fy))
        dx = st["drift"] * cw * (np.sin(2 * np.pi * S / 9.0 + phase) + 0.5 * np.sin(2 * np.pi * S / 4.1 + 2 * phase)) / 1.5
        dy = st["drift"] * ch * 0.6 * np.sin(2 * np.pi * S / 7.3 + 3 * phase)
        x0 = cx + dx - cw / 2
        y0 = cy + dy - st["face_y"] * ch
        return (min(max(0.0, x0), TW - cw), min(max(0.0, y0), TH - ch), cw, ch)

    # --- captions go under the mouth: a steady position from how low the mouth usually gets, and a
    #     line said while the speaker leans lower drops further down (never over the lips)
    mouth = []
    for S in np.arange(0, dur + 0.1, 0.1):
        t_src = tl.to_src(min(S, dur - 1e-3))
        x0, y0, cw, ch = crop(S, t_src)
        my = float(np.interp(t_src, ft, fy)) + st["mouth_below_face"] * float(np.interp(t_src, ft, fh))
        mouth.append((my - y0) / ch * out_h)
    mouth = np.array(mouth)
    cap_h = st["caption_size"] * 1.25

    def margin_for(mouth_y):
        return int(out_h - (mouth_y + st["caption_mouth_gap"]) - cap_h)

    margin_v = int(min(st["caption_margin_max"], max(st["caption_margin_min"], margin_for(np.percentile(mouth, st["caption_mouth_pct"])))))
    strap_clear = st["strap_bottom"] + strap.h + st["strap_gap"]  # captions above the strap while it's up
    lines = caption_line_groups(words, st)
    margins, lowered, clash = [], 0, []
    for ln in lines:
        a0, b0 = ln[0]["start"], ln[-1]["end"]
        room = margin_for(mouth[int(max(0, a0) * 10):int(b0 * 10) + 2].max())  # highest the line may sit
        mv = margin_v if room >= margin_v else max(room, st["caption_margin_floor"])
        if room < st["caption_margin_floor"]:
            print(f"[{tag}] WARNING: at {a0:.1f}s the mouth is so low the caption can't clear it")
        if a0 < s_out + st["strap_fade"] and b0 > s_in:  # the strap is up: the line sits just above it
            if room >= strap_clear:
                mv = max(mv, strap_clear)
            else:
                clash.append(a0)
        lowered += mv < margin_v
        margins.append(int(mv))
    if clash:  # the mouth is too low to fit captions above the strap: the strap leaves before those lines
        s_out = max(s_in + 1.0, min(clash) - st["strap_fade"])
        print(f"[{tag}] strap shortened to {s_out - s_in:.1f}s: the mouth is low at {min(clash):.1f}s")
    # the strap sits right under the captions it shares the screen with (the lowest of them)
    up = [mv for ln, mv in zip(lines, margins) if ln[0]["start"] < s_out + st["strap_fade"] and ln[-1]["end"] > s_in]
    if up:
        strap.y = min(out_h - st["strap_bottom"] - strap.h, out_h - min(up) + st["strap_gap"])

    out_dir = edir / "tileshort"
    out_dir.mkdir(exist_ok=True)
    plan = {"range_src": [a, b], "parts_src": ranges, "hook_src": [ha, hb], "segments": segs,
            "duration": round(dur, 2), "mouth_max_y": round(float(mouth.max())), "caption_margin_v": margin_v,
            "caption_margins": margins, "strap": [s_in, s_out, strap.x, strap.y, strap.w, strap.h],
            "crops": [[round(S, 2)] + [round(v, 1) for v in crop(S, tl.to_src(S))] for S in np.arange(0, dur, 0.5)]}
    (out_dir / f"{tag}.plan.json").write_text(json.dumps(plan, indent=1))
    ass = out_dir / f"{tag}.ass"
    write_ass(ass, lines, st, dur, hi, margins)
    raw = sum(y - x for x, y in ranges)
    print(f"[{tag}] {dur:.1f}s ({raw:.1f}s before tightening, {len(ranges)} parts{', hook' if ha is not None else ''}), "
          f"{len(tl.chops())} jump cuts, strap {s_in:.1f}-{s_out:.1f}s"
          + f", centring zoom up to {need.max():.2f}x, mouth down to y={mouth.max():.0f}, captions margin {margin_v} ({lowered} lines lowered); target {spec.get('target', '?')}")
    if dur > st["max_seconds"] + 0.05:
        print(f"[{tag}] FAIL: {dur:.1f}s is over the {st['max_seconds']}s limit. Not rendered.")
        return None
    if plan_only:
        return out_dir / f"{tag}.plan.json"

    vignette = None
    if st.get("vignette"):
        yy, xx = np.mgrid[0:out_h, 0:out_w].astype(np.float32)
        r = np.sqrt(((xx - out_w / 2) / (out_w / 2)) ** 2 + ((yy - out_h / 2) / (out_h / 2)) ** 2) / np.sqrt(2)
        vignette = (1 - st["vignette"] * np.clip(r - 0.35, 0, 1) ** 2 / 0.42)[..., None].astype(np.float32)
    grade = re.sub(r",?vignette=[^,]*", "", st.get("grade") or "")  # a frame vignette would darken one corner of the tile

    with tempfile.TemporaryDirectory() as tmp:
        full = ed.audio()
        audio = tighten.splice(full, [(int(sg[0] * tighten.SR), int(sg[1] * tighten.SR)) for sg in segs])
        head, tail = int(0.005 * tighten.SR), int(0.06 * tighten.SR)
        audio[:head] *= np.linspace(0, 1, head, dtype=np.float32)
        audio[-tail:] *= np.linspace(1, 0, tail, dtype=np.float32)
        wav = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(tighten.SR), "-ac", "1",
                        "-i", "-", "-c:a", "pcm_f32le", str(wav)], input=audio.astype(np.float32).tobytes(), check=True)
        out = out_dir / f"{tag}.mp4"
        sub = str(ass).replace(":", "\\:")
        enc = subprocess.Popen(
            [ffmpeg_with_libass(), "-nostdin", "-v", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "-",
             "-i", str(wav), "-filter_complex", f"[0:v]subtitles=filename='{sub}'[v]",
             "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)],
            stdin=subprocess.PIPE)
        frames = int(round(dur * fps))
        dec, dec_seg = None, -1
        began = time.time()
        still_at = {int(round(x * fps)) for x in (0.5, 2.5, dur / 2, dur - 1)}
        try:
            for n in range(frames):
                S = n / fps
                k = tl.seg_at(S)
                if k != dec_seg:
                    if dec:
                        dec.close()
                    src0, src1 = ed.to_src(segs[k][0]), ed.to_src(segs[k][1] - 1e-3)
                    dec = TileDecoder(ev, who.get("camera", "cam1"), tile, pstart + src0, src1 - src0, fps, grade)
                    dec_seg = k
                t_src = tl.to_src(S)
                src = dec.at(pstart + t_src)
                if mask_box:  # paint the call's name tag out (only its box, so it stays cheap)
                    mx, my_, mw, mh = mask_box
                    pad = 12
                    X0, Y0 = max(0, mx - pad), max(0, my_ - pad)
                    X1, Y1 = min(src.shape[1], mx + mw + pad), min(src.shape[0], my_ + mh + pad)
                    src = src.copy()
                    src[Y0:Y1, X0:X1] = cv2.inpaint(src[Y0:Y1, X0:X1], tag_mask[Y0:Y1, X0:X1], 6, cv2.INPAINT_TELEA)
                x0, y0, cw, ch = crop(S, t_src)
                m = np.float32([[out_w / cw, 0, -x0 * out_w / cw], [0, out_h / ch, -y0 * out_h / ch]])
                frame = cv2.warpAffine(src, m, (out_w, out_h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
                if st.get("sharpen"):
                    blur = cv2.GaussianBlur(frame, (0, 0), 2.2)
                    frame = cv2.addWeighted(frame, 1 + st["sharpen"], blur, -st["sharpen"], 0)
                if vignette is not None:
                    frame = (frame.astype(np.float32) * vignette).astype(np.uint8)
                title_box.draw(frame)
                sa = min(ease_io((S - s_in) / st["strap_fade"]), 1 - ease_io((S - s_out) / st["strap_fade"]))
                if sa > 0:  # the strap rises in a little as it fades in
                    y_keep = strap.y
                    strap.y = int(y_keep + st["strap_rise"] * (1 - ease_out(min(1.0, (S - s_in) / st["strap_fade"]))))
                    strap.draw(frame, sa)
                    strap.y = y_keep
                if st.get("progress_height"):
                    lf.draw_progress(frame, S / dur, st)
                enc.stdin.write(frame.tobytes())
                if stills and n in still_at:
                    cv2.imwrite(str(out_dir / f"{tag}.f{n:04d}.jpg"), frame)
        finally:
            enc.stdin.close()
            enc.wait()
            if dec:
                dec.close()
    if enc.returncode:
        sys.exit(f"ffmpeg failed ({enc.returncode})")
    print(f"[{tag}] rendered in {time.time() - began:.0f}s -> {out.relative_to(edir)}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("panel")
    ap.add_argument("--id", action="append", help="only these shorts (repeatable)")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--stills", action="store_true", help="also save a few rendered frames per short")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    p = panel(ev, args.panel)
    story = YAML(typ="safe").load((edir / "longform" / f"{p['id']}.story.yaml").read_text()) or {}
    specs = [s for s in story.get("tile_shorts") or [] if not args.id or s["id"] in args.id]
    if not specs:
        sys.exit("no tile_shorts in the story file (or none match --id)")
    for spec in specs:
        render(ev, edir, p, spec, args.plan_only, args.stills)


if __name__ == "__main__":
    main()
