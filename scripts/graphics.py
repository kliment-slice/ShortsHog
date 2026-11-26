"""On-screen graphics shared by the shorts (shortform.py, tileshort.py): the frosted title, the lower-third
name strap and the outlined captions with the word being said lit in a PostHog colour.

Graphics are drawn once with PIL into RGBA layers and composited onto each BGR frame; captions are
an ASS file that ffmpeg burns in. Sizes come from the style block of the caller ("## Short form" or
"## Tile short" in FORMAT.md): title_*, strap_*, caption_*.
"""
import re
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import longform as lf

FONT_DIR = Path.home() / "Library" / "Fonts"


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


def color_of(name: str, st: dict) -> str:
    """A PostHog colour by name (st["colors"]) or a #RRGGBB as is."""
    return st["colors"].get(name, name)


class Title:
    """The headline on a frosted box at the top, on screen from frame 1: the footage behind it blurred and
    tinted (live, every frame), the text in a heavier RoundHog than the captions, left-aligned, the key
    phrase on a highlighter bar in the short's colour. It reads as a title, not a subtitle."""

    def __init__(self, headline: str, key: str, st: dict, color: str, tag: str = ""):
        w = st["size"][0]
        m, pad = st["title_margin"], st["title_pad"]
        f = font(st["title_weight"], st["title_size"])
        words = lf.clean(headline).split()
        kw = lf.clean(key or "").split()
        nrm = lambda x: re.sub(r"\W", "", x).lower()
        lo = next((i for i in range(len(words)) if [nrm(x) for x in words[i:i + len(kw)]] == [nrm(x) for x in kw]),
                  None) if kw else None
        if kw and lo is None:
            print(f"[{tag}] WARNING: key {' '.join(kw)!r} isn't in the headline")
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
        self.lines = len(lines)
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

    def draw(self, frame: np.ndarray, opacity: float = 1.0) -> None:
        if opacity <= 0:
            return
        x0, y0, x1, y1 = self.box
        roi = frame[y0:y1, x0:x1]
        small = cv2.resize(roi, (max(1, (x1 - x0) // 4), max(1, (y1 - y0) // 4)), interpolation=cv2.INTER_AREA)
        frost = cv2.resize(cv2.GaussianBlur(small, (0, 0), self.blur / 4), (x1 - x0, y1 - y0),
                           interpolation=cv2.INTER_LINEAR).astype(np.float32)
        frost = frost * (1 - self.tint_a) + self.tint * self.tint_a
        m = self.mask * opacity
        frame[y0:y1, x0:x1] = (roi * (1 - m) + frost * m).astype(np.uint8)
        self.text.draw(frame, opacity)


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


def _breaks(n_words: int, n_lines: int):
    """Every way to cut n_words into n_lines non-empty runs, as the start index of lines 2..n."""
    from itertools import combinations
    return combinations(range(1, n_words), n_lines - 1)


class SummaryBand:
    """The summary across the top of the frame (summary_height), like the PostHog thumbnail generator: a solid brand
    colour, the small white PostHog logo top left, the quote in white ExtraBold, left-aligned, sized to fit,
    the key phrase on soft white highlight boxes (one per line it runs over), then "- Name, role at company"
    in dark text on its own line under the quote. The quote's lines are balanced (no orphans or widows,
    breaks after punctuation where possible) at the biggest size that fits.
    Same layout on every short; only the colour changes."""

    def __init__(self, headline: str, key: str, st: dict, color: str, logo: Path, tag: str = "",
                 name: str = "", role: str = ""):
        w, h = st["size"][0], int(round(st["size"][1] * st["summary_height"]))
        m = st["summary_margin"]
        self.st = st
        mark = Image.open(logo).convert("RGBA")
        lh_ = st["summary_logo_height"]
        mark = mark.resize((int(mark.width * lh_ / mark.height), lh_), Image.LANCZOS)
        self.logo = Layer(mark, m, st["summary_logo_top"])
        content = Image.new("RGBA", (w, h), (0, 0, 0, 0))  # logo, highlights and words at rest: the drawer scales it
        content.alpha_composite(mark, (m, st["summary_logo_top"]))
        word_ink = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        words = ["\u201c" + lf.clean(headline).split()[0]] + lf.clean(headline).split()[1:]
        words[-1] += "\u201d"
        kw = lf.clean(key or "").split()
        nrm = lambda x: re.sub(r"\W", "", x).lower()
        lo = next((i for i in range(len(words)) if [nrm(x) for x in words[i:i + len(kw)]] == [nrm(x) for x in kw]),
                  None) if kw else None
        if kw and lo is None:
            print(f"[{tag}] WARNING: title_key {' '.join(kw)!r} isn't in the summary")
        keyed = set(range(lo, lo + len(kw))) if lo is not None else set()
        top = st["summary_logo_top"] + lh_ + st["summary_logo_gap"]
        room_h, room_w = h - top - st["summary_bottom_pad"], w - 2 * m
        fn, fr = font("Bold", st["summary_name_size"]), font("SemiBold", st["summary_role_size"])
        name = f"- {name}" if name else ""  # its own line under the quote: "- Name, role at company"
        role = f", {role}" if name and role else ""
        bw = int(fn.getlength(name) + fr.getlength(role)) if name else 0
        bh = max(st["summary_name_size"], st["summary_role_size"]) if name else 0
        if bw > room_w:
            print(f"[{tag}] WARNING: the name and role are wider than the band")
        below = (st["summary_byline_below"] + bh) if name else 0
        best = None
        # first try breaking only after punctuation (the quote's own phrasing), then any balanced break
        for strict in (True, False):
            if best:
                break
            for size in range(st["summary_size_max"], st["summary_size_min"] - 1, -2):  # the biggest that fits
                f = font(st["summary_weight"], size)
                space = f.getlength(" ")
                ws = [f.getlength(x) for x in words]
                lh = int(size * st["summary_leading"])
                for n in range(1, st["summary_max_lines"] + 1):
                    if lh * (n - 1) + size + below > room_h:
                        break
                    for cut in _breaks(len(words), n):
                        lines = [list(range(a, b)) for a, b in zip((0,) + cut, cut + (len(words),))]
                        widths = [sum(ws[k] for k in ln) + space * (len(ln) - 1) for ln in lines]
                        if max(widths) > room_w:
                            continue
                        # no orphans or widows: every line two words or more and not much shorter than the longest
                        if n > 1 and (min(map(len, lines)) < 2 or min(widths) < st["summary_min_line"] * max(widths)):
                            continue
                        ragged = sum((max(widths) - x) ** 2 for x in widths) / max(widths) ** 2
                        punct = sum(0 if re.search(r"[,.;:?!]\W*$", words[ln[-1]]) else 1 for ln in lines[:-1])
                        if strict and punct:
                            continue
                        cost = ragged + st["summary_break_penalty"] * punct
                        if best is None or cost < best[0]:
                            best = (cost, lines, widths)
                if best:
                    break
        if not best:
            print(f"[{tag}] WARNING: the summary doesn't fit the band without a widow even at {size}px")
            f = font(st["summary_weight"], size)
            space, lh = f.getlength(" "), int(size * st["summary_leading"])
            best = (0, [list(range(len(words)))], [sum(f.getlength(x) for x in words)])
        lines = [[(k, words[k], f.getlength(words[k])) for k in ln] for ln in best[1]]
        text_h = lh * (len(lines) - 1) + size
        self.size, self.lines = size, len(lines)
        hp, rad = st["summary_highlight_pad"], st["summary_highlight_radius"]
        hl = (255, 255, 255, int(255 * st["summary_highlight_alpha"]))
        total_h = text_h + below
        y0 = top + (room_h - total_h) // 2  # centred in the space under the logo
        asc = f.getbbox("Hg")
        pad = 8  # room around each word image for the glyphs' overhang
        stagger, n_words = st["summary_reveal_stagger"], len(words)
        self.words, self.highlights = [], []
        for n, line in enumerate(lines):
            x, y = m, y0 + n * lh
            run = None
            for i, word, ww in line:
                core = word.rstrip("\u201d") if i == n_words - 1 else word  # the closing quote sits outside
                if i in keyed:
                    run = [x, x + f.getlength(core)] if run is None else [run[0], x + f.getlength(core)]
                im = Image.new("RGBA", (int(ww) + 2 * pad, lh + 2 * pad), (0, 0, 0, 0))
                ImageDraw.Draw(im).text((pad, pad), word, font=f, fill=(255, 255, 255, 255))
                self.words.append((Layer(im, int(x) - pad, y - pad), i * stagger))
                word_ink.alpha_composite(im, (max(0, int(x) - pad), max(0, y - pad)))
                x += ww + space
            if run:  # the highlighter wipes in left to right once the words are up
                x0, y_0 = int(run[0] - hp), int(y + asc[1] - hp)
                x1, y_1 = int(run[1] + hp), int(y + asc[3] + hp)
                im = Image.new("RGBA", (x1 - x0, y_1 - y_0), (0, 0, 0, 0))
                ImageDraw.Draw(im).rounded_rectangle([0, 0, x1 - x0 - 1, y_1 - y_0 - 1], radius=rad, fill=hl)
                self.highlights.append(Layer(im, x0, y_0))
                content.alpha_composite(im, (max(0, x0), max(0, y_0)))
        self.highlight_at = n_words * stagger + st["summary_reveal_dur"] * 0.6
        self.byline = None
        if name:  # dark text: black at an alpha, so it takes the band's colour (like the thumbnail generator)
            base = y0 + text_h + st["summary_byline_below"] + bh  # name and role share a baseline
            im = Image.new("RGBA", (w, bh + 20), (0, 0, 0, 0))
            di = ImageDraw.Draw(im)
            di.text((m, bh + 8), name, font=fn, anchor="ls", fill=(0, 0, 0, int(255 * st["summary_name_alpha"])))
            if role:
                di.text((m + fn.getlength(name), bh + 8), role, font=fr, anchor="ls",
                        fill=(0, 0, 0, int(255 * st["summary_role_alpha"])))
            self.byline = Layer(im, 0, base - bh - 8)
        # the band is the footage behind it, blurred and washed in the brand colour, and it fades into the
        # picture over summary_fade px instead of stopping at a hard edge
        fade = st["summary_fade"]
        y = np.arange(h + fade, dtype=np.float32)
        u = np.clip((y - h) / max(1, fade), 0, 1)
        self.ramp = (1 - (3 * u * u - 2 * u ** 3))[:, None, None]  # 1 in the band, easing to 0 below it
        self.color = np.array(lf.bgr(color), np.float32)
        self.h, self.w = h, w
        self.box = (0, 0, w, h)
        # the drawer: once the name has gone, the band closes to summary_compact_height and the quote (with the
        # logo) shrinks with it, anchored on the left margin
        content.alpha_composite(word_ink)
        self.content = np.asarray(content).astype(np.float32)
        self.hc = int(round(st["size"][1] * st["summary_compact_height"]))
        self.k = self.hc / h  # everything shrinks by the same ratio on every short, so the drawer feels the same
        top_c, bot_c = self.k * st["summary_logo_top"], self.k * (y0 + text_h)
        self.dy = max(0.0, (self.hc - bot_c - top_c) / 2)  # closed, the content sits centred in the band
        self.closing = st["summary_byline_seconds"] + st["summary_byline_fade"] if name else st["summary_byline_seconds"]
        self._cache = {}

    def _scaled(self, k: float, dy: float = 0.0) -> Layer:
        """The band's content at scale k, its left margin kept where it was, moved down by dy."""
        key = (round(k, 3), int(round(dy)))
        if key not in self._cache:
            m = self.st["summary_margin"]
            if k >= 0.999:
                a = self.content
            else:
                a = cv2.resize(self.content, (max(1, int(self.w * k)), max(1, int(self.h * k))), interpolation=cv2.INTER_AREA)
            lay = Layer.__new__(Layer)
            lay.bgr, lay.alpha = a[..., [2, 1, 0]], a[..., 3:4] / 255.0
            lay.h, lay.w = a.shape[:2]
            lay.x, lay.y = int(round(m - m * k)), int(round(dy))
            if lay.x + lay.w > self.w:
                lay.w = self.w - lay.x
                lay.bgr, lay.alpha = lay.bgr[:, :lay.w], lay.alpha[:, :lay.w]
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = lay
        return self._cache[key]

    def draw(self, frame: np.ndarray, S: float = 99.0) -> None:
        st, h, w = self.st, self.h, self.w
        c = ease_io((S - self.closing) / st["summary_collapse_dur"])  # 0 open, 1 closed
        hb = int(round(h + (self.hc - h) * c))
        fade = st["summary_fade"]
        H = hb + fade
        y = np.arange(H, dtype=np.float32)
        u = np.clip((y - hb) / max(1, fade), 0, 1)
        ramp = (1 - (3 * u * u - 2 * u ** 3))[:, None, None]  # 1 in the band, easing to 0 below it
        roi = frame[:H].astype(np.float32)
        small = cv2.resize(roi, (w // 8, max(1, H // 8)), interpolation=cv2.INTER_AREA)
        blur = cv2.resize(cv2.GaussianBlur(small, (0, 0), st["summary_blur"] / 8), (w, H),
                          interpolation=cv2.INTER_LINEAR)
        wash = blur * (1 - st["summary_tint_alpha"]) + self.color * st["summary_tint_alpha"]
        frame[:H] = (roi * (1 - ramp) + wash * ramp).astype(np.uint8)
        if c > 0:  # the words are long up: the whole content shrinks with the drawer
            self._scaled(1 + (self.k - 1) * c, self.dy * c).draw(frame)
            return
        self.logo.draw(frame)
        hp = min(1.0, max(0.0, (S - self.highlight_at) / st["summary_highlight_dur"]))
        if hp > 0:
            e = 1 - (1 - hp) ** 3
            for lay in self.highlights:  # wipe: only the left part of the box so far
                cut = max(1, int(lay.w * e))
                part = Layer.__new__(Layer)
                part.bgr, part.alpha = lay.bgr[:, :cut], lay.alpha[:, :cut]
                part.x, part.y, part.h, part.w = lay.x, lay.y, lay.h, cut
                part.draw(frame)
        dur, rise = st["summary_reveal_dur"], st["summary_reveal_rise"]
        for lay, t0 in self.words:  # each word rises in and fades up, one after another
            u = min(1.0, max(0.0, (S - t0) / dur))
            if u <= 0:
                continue
            e = 1 - (1 - u) ** 3
            y_keep = lay.y
            lay.y = int(y_keep + rise * (1 - e))
            lay.draw(frame, e)
            lay.y = y_keep
        if self.byline:  # the name and role come in with the quote and leave after summary_byline_seconds
            a = min(ease_io((S - self.highlight_at) / st["summary_byline_fade"]),
                    1 - ease_io((S - st["summary_byline_seconds"]) / st["summary_byline_fade"]))
            self.byline.draw(frame, a)


class FrostStrap:
    """A subtle lower third: the name over the role, left-aligned on a small frosted box (the footage behind
    it blurred and tinted, like the title), no solid colour. Placed by the caller (x, y)."""

    def __init__(self, name: str, role: str, st: dict, tint: str | None = None):
        fn, fr = font("Bold", st["strap_name_size"]), font("SemiBold", st["strap_role_size"])
        px, py, gap = st["strap_pad_x"], st["strap_pad_y"], st.get("strap_line_gap", 4)
        w = int(max(fn.getlength(name), fr.getlength(role) if role else 0) + 2 * px)
        h = st["strap_name_size"] + (gap + st["strap_role_size"] if role else 0) + 2 * py + 6
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=st["strap_radius"],
                            outline=(255, 255, 255, int(255 * st["title_border_alpha"])), width=2)
        d.text((px, py - 2), name, font=fn, fill=(255, 255, 255, 255))
        if role:
            d.text((px, py + st["strap_name_size"] + gap), role, font=fr,
                   fill=(255, 255, 255, int(255 * st.get("strap_role_alpha", 0.78))))
        self.text = Layer(img, 0, 0)
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=st["strap_radius"], fill=255)
        self.mask = (np.asarray(mask, np.float32) / 255)[..., None]
        self.tint = np.array(lf.bgr(tint or st["title_tint"]), np.float32)
        self.tint_a, self.blur = st.get("strap_tint_alpha", st["title_tint_alpha"]), st["title_blur"]
        self.w, self.h, self.x, self.y = w, h, 0, 0

    def draw(self, frame: np.ndarray, opacity: float = 1.0) -> None:
        if opacity <= 0:
            return
        x0, y0 = self.x, self.y
        roi = frame[y0:y0 + self.h, x0:x0 + self.w]
        small = cv2.resize(roi, (max(1, self.w // 4), max(1, self.h // 4)), interpolation=cv2.INTER_AREA)
        frost = cv2.resize(cv2.GaussianBlur(small, (0, 0), self.blur / 4), (self.w, self.h),
                           interpolation=cv2.INTER_LINEAR).astype(np.float32)
        frost = frost * (1 - self.tint_a) + self.tint * self.tint_a
        m = self.mask * opacity
        frame[y0:y0 + self.h, x0:x0 + self.w] = (roi * (1 - m) + frost * m).astype(np.uint8)
        self.text.x, self.text.y = x0, y0
        self.text.draw(frame, opacity)


def draw_strap(frame: np.ndarray, strap, S: float, s_in: float, s_out: float, st: dict) -> None:
    """The strap fades in rising a little at s_in and fades out at s_out."""
    sa = min(ease_io((S - s_in) / st["strap_fade"]), 1 - ease_io((S - s_out) / st["strap_fade"]))
    if sa > 0:
        y_keep = strap.y
        strap.y = int(y_keep + st["strap_rise"] * (1 - ease_out(min(1.0, (S - s_in) / st["strap_fade"]))))
        strap.draw(frame, sa)
        strap.y = y_keep


def caption_line_groups(words: list, st: dict) -> list:
    """Words grouped into caption lines: a new speaker, a pause, a full line, or punctuation ends one."""
    lines, cur = [], None
    for x in words:
        if cur is not None:
            text = " ".join(y["w"] for y in cur)
            if (x["seg"] != cur[-1]["seg"] or x.get("speaker") != cur[-1].get("speaker")
                    or x["start"] - cur[-1]["end"] > 0.5
                    or len(text) + 1 + len(x["w"]) > st["caption_max_chars"]
                    or re.search(r"[.?!,]$", text) and len(text) > 8):
                lines.append(cur)
                cur = None
        cur = [x] if cur is None else cur + [x]
    return lines + ([cur] if cur else [])


def write_captions(path: Path, lines: list, st: dict, dur: float, hi: str, margins: list,
                   prefixes: list | None = None) -> None:
    """Captions with no box: white RoundHog with a black outline, the word being said in `hi` (a PostHog
    colour). One event per word, so exactly one word is lit at a time, and a line ends where the next
    starts (never two lines on screen). margins[k]: line k's MarginV. prefixes[k]: optional ASS text put
    before line k (e.g. another speaker's name in their colour)."""
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
        pre = (prefixes[k] if prefixes else "") or ""
        for i, x in enumerate(ln):
            a = ln[0]["start"] if i == 0 else x["start"]
            b = ln[i + 1]["start"] if i + 1 < len(ln) else end
            if b - a < 0.01 or a >= dur:
                continue
            pop = st.get("caption_pop", 1.0)
            if pop != 1.0:  # the word being said grows a touch as its colour comes in
                big = int(round(pop * 100))
                lit = (f"{{\\c{lf.ass_color(hi)}\\fscx100\\fscy100\\t(0,{st.get('caption_pop_ms', 90)},"
                       f"\\fscx{big}\\fscy{big})}}")
                unlit = f"{{\\c{white}\\fscx100\\fscy100}}"
            else:
                lit, unlit = f"{{\\c{lf.ass_color(hi)}}}", f"{{\\c{white}}}"
            body = " ".join(f"{lit}{t}{unlit}" if j == i else t for j, t in enumerate(texts))
            ev.append(f"Dialogue: 0,{lf.ass_time(max(0.0, a))},{lf.ass_time(b)},Caption,,0,0,{margins[k]},,{pre}{body}")
    path.write_text(head + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                    + "\n".join(ev) + "\n")
