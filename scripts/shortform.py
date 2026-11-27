"""Cut 3:4 portrait shorts in the ShortsHog look: claim (the hook), context, evidence, payoff.

    .venv/bin/python scripts/shortform.py <event> <panel> [--id SHORT_ID ...] [--plan-only]

Specs live under `shorts:` in events/<event>/longform/<panel>.story.yaml:
    {id, speaker, hook, from, to, payoff?, skip?, react?, repeat_hook?}
All are transcript text. `hook` is the claim the short opens on, as said (the anchor sentence);
`from` / `to` the meat (the hook is cut from it unless `repeat_hook`); `payoff` the sentence it
must end on (checked); `skip` phrases are cut; `react: [{at, who, for?}]` hand-picked reaction
shots (otherwise the stillest listener reacts once the hook has finished; `auto_react: false`
turns that off). Style and the selection rubric: "## Short form" in FORMAT.md, edit settings
"## Short-form edit".

  - Tightened harder than the long form: every um cut, pauses down to a breath, restarts
    and standalone "you know" / "right?" cut, plus the panel's editorial cuts. Audio joins
    are 15 ms crossfades.
  - Camera: the speaker's close-up (~2/3 of the frame, eyes on the upper third, following
    their tracked face on cam2), held still, tighter through the hook. It changes only on a
    trigger (`cutaway_triggers`) at a sentence boundary: a listener's reaction once the hook
    has finished, someone else talking for a while (l_cut into their first word), or the
    framing stepping once it has held shot_max. Jump cuts punch the framing in or out.
  - On screen: frame 1 is the hook's first word, a hard cut to the meat. The headline (`headline`,
    default `anchor`) on a frosted box at the top from frame 1, `title_key` on a highlighter bar in
    the short's `color`; captions with no box (white, black outline, the word being said lit); a
    lower-third name strap just under them once the sweep lands; the tangerine progress bar.
    No title card, no wipe.
  - Audience cutaways: the room listening (event.yaml panel `audience`, b-roll filmed during this
    panel), cut in over the speaker's audio at a sentence start, video only.
  - Anchor graphics: each `beats:` entry with an `asset` (overlays.py fetch) shows a short
    stock clip (or a graphic) on a track above the footage, kept off faces, the title, the captions and the strap;
    its `pos` is written back to the spec (--replace places them all again).
Output: events/<event>/shortform/<id>.mp4 (+ .ass, .plan.json).
"""
import argparse
import json
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
from ruamel.yaml.comments import CommentedSeq

import edit as tighten
import graphics as gx
import longform as lf
import overlays as ov
from common import REPO, event_dir, ffmpeg_with_libass, load_event, media, output_fps, panel, panel_range, probe, synced


def style() -> dict:
    text = (REPO / "FORMAT.md").read_text()
    m = re.search(r"^## Short form\n.*?```yaml\n(.*?)```", text, re.S | re.M)
    if not m:
        sys.exit("FORMAT.md needs a ```yaml block under '## Short form'")
    return {**lf.style(), **YAML(typ="safe").load(m.group(1))}


def find_in(words: list, phrase: str, a: float, b: float) -> list:
    """[(start, end)] of each occurrence of phrase whose words lie inside [a, b]."""
    live = [w for w in words if not w.get("silent") and w.get("speaker") and a - 0.01 <= w["start"] <= b + 0.01]
    toks = [tighten.norm(w["w"]) for w in live]
    q = [t for t in (tighten.norm(x) for x in phrase.split()) if t]
    out = []
    for i in range(len(toks) - len(q) + 1):
        if toks[i:i + len(q)] == q:
            out.append((live[i]["start"], tighten.end_before_next(live, i + len(q) - 1)))
    return out


# ---------------------------------------------------------------- timeline

class Timeline:
    """The short as segments of the tightened panel: [(T0, T1, kind)] in edit output time, or
    (T0, T1, kind, src_lo, src_hi) when a segment's words must come from that source range (a word swap)."""

    def __init__(self, segs: list, ed):
        self.segs, self.ed = segs, ed
        self.offsets = np.cumsum([0.0] + [s[1] - s[0] for s in segs]).tolist()
        self.dur = self.offsets[-1]

    def seg_at(self, S: float) -> int:
        return min(max(0, bisect_right(self.offsets, S) - 1), len(self.segs) - 1)

    def to_T(self, S: float) -> float:
        k = self.seg_at(S)
        return self.segs[k][0] + (S - self.offsets[k])

    def to_src(self, S: float) -> float:
        return self.ed.to_src(self.to_T(S))

    def to_S(self, T: float) -> float | None:
        for k, (a, b, *_) in enumerate(self.segs):
            if a - 1e-3 <= T <= b + 1e-3:
                return self.offsets[k] + (T - a)
        return None

    def words(self, words_out: list) -> list:
        out = []
        for k, (a, b, _, *src) in enumerate(self.segs):
            for w in words_out:
                inside = (src[0] - 0.01 <= w["src"][0] < src[1] - 0.01 and a - 0.3 <= w["start"] < b - 0.02) if src else \
                    a - 0.02 <= w["start"] < b - 0.02
                if inside:
                    out.append({**w, "start": round(max(self.offsets[k], self.offsets[k] + w["start"] - a), 3),
                                "end": round(min(self.offsets[k] + w["end"] - a, self.offsets[k + 1]), 3), "seg": k})
        return out

    def chops(self) -> list:
        """Short times where the picture jumps: the edit's cuts inside segments, and the joins."""
        out = []
        for k, (a, b, *_) in enumerate(self.segs):
            out += [self.offsets[k] + c - a for c in self.ed.chops() if a + 0.05 < c < b - 0.05]
        return sorted(out + self.offsets[1:-1])


def padded(ed, words_src: list, a: float, b: float, st: dict, pad_start: float | None = None) -> tuple:
    """Edit output range of source words [a, b], padded but never into the words either side."""
    a_out, b_out = ed.to_out_near(a), ed.to_out_near(b)
    i = next(k for k, w in enumerate(words_src) if abs(w["start"] - a) < 0.005)
    j = max(k for k in range(i, len(words_src)) if words_src[k]["start"] < b)  # b may be clamped before its end
    before = [w["end"] for w in ed.words_out(words_src[:i])]
    after = [w["start"] for w in ed.words_out(words_src[j + 1:])]
    t0 = max(0.0, a_out - (st["pad_start"] if pad_start is None else pad_start),
             min(a_out, before[-1] + 0.03) if before else 0.0)
    # whisper often stretches a range's last word over the pause (or an um) after it: end at the first
    # 0.2 s of quiet (10 dB under the panel's speech level) after the word starts
    last = words_src[j]
    i0, i1 = int((last["start"] + 0.2) * tighten.ENV_HZ), int(min(b, last["end"]) * tighten.ENV_HZ)
    quiet = ed.env[i0:i1] < np.percentile(ed.env, 90) - 10
    run = next((k for k in range(len(quiet) - 19) if quiet[k:k + 20].all()), None)
    if run is not None:
        b_out = min(b_out, ed.to_out_near((i0 + run) / tighten.ENV_HZ + 0.05))
    t1 = min(ed.out_dur, b_out + st["pad_end"], max(b_out, after[0] - 0.05) if after else ed.out_dur)
    return t0, t1


def scan_fillers(edir: Path, panel_id: str, ranges: list, cfg: dict, tag: str, words_src: list) -> list:
    """Ums whisper's first pass left out (it tends to drop them or fold them into the word before): a
    second pass over each range, prompted to write fillers down verbatim, with DTW word times. Each
    filler it hears becomes a cut [(start, end, "um", "")] up to the next word. Cached per range in
    transcripts/<panel>.fillers.json."""
    import transcribe as tr
    cache_path = edir / "transcripts" / f"{panel_id}.fillers.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    fillers = set(cfg["fillers"])
    out = []
    for lo, hi in ranges:
        key = f"{lo:.2f}-{hi:.2f}"
        if key not in cache:
            pcm = tighten.decode_audio(edir / "transcripts" / f"{panel_id}.wav", tr.WHISPER_SR)

            def second_pass(a: float, b: float) -> list:
                with tempfile.TemporaryDirectory() as tmp:
                    f = Path(tmp) / "part.wav"
                    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(tr.WHISPER_SR),
                                    "-ac", "1", "-i", "-", "-c:a", "pcm_s16le", str(f)],
                                   input=pcm[int(a * tr.WHISPER_SR):int(b * tr.WHISPER_SR)].tobytes(), check=True)
                    r = subprocess.run(["whisper-cli", "-m", str(tr.MODEL), "-l", "en", "-ojf", "-dtw", "large.v3.turbo",
                                        "-nfa", "-np", "--prompt", "Um, so, uh, I mean, um, we, uh, like, um.", "-f", str(f)],
                                       capture_output=True)
                    if r.returncode == 0:
                        return tr.words_from_data(json.loads(Path(f"{f}.json").read_text()), a)[0]
                # whisper's DTW timing can abort on a clip right at its 30 s window: do it in halves
                if b - a < 10:
                    raise RuntimeError(f"whisper-cli failed on {a:.2f}-{b:.2f}s (exit {r.returncode})")
                print(f"[{tag}] filler scan: whisper failed on {a:.1f}-{b:.1f}s, retrying in halves")
                m = (a + b) / 2
                return [w for w in second_pass(a, m + 0.5) if w["start"] < m] + \
                       [w for w in second_pass(m - 0.5, b) if w["start"] >= m]

            words = second_pass(max(0.0, lo - 0.5), hi + 0.5)
            cache[key] = [{"w": w["w"], "start": w["start"], "end": w["end"]} for w in words]
            cache_path.write_text(json.dumps(cache, indent=1))
        real = sorted((x["start"], tighten.norm(x["w"])) for x in words_src
                      if x.get("speaker") and tighten.norm(x["w"]) not in fillers)
        # every cached pass that covers this range: each pass hears a different few ums
        passes = [v for k, v in cache.items()
                  if float(k.split("-")[0]) < hi and float(k.split("-")[1]) > lo]
        found = []
        for words in passes:
            for i, w in enumerate(words):
                if tighten.norm(w["w"]) not in fillers or not lo - 0.05 <= w["start"] <= hi:
                    continue
                # never into a real word: stop at the next transcribed word, start after the one before
                nxt = next(((t, n) for t, n in real if t > w["start"] + 0.05), (w["start"] + 0.9, ""))
                prev_real = max((t for t, _ in real if t <= w["start"] - 0.05), default=0.0)
                after = words[i + 1] if i + 1 < len(words) else None
                start = max(w["start"], prev_real + 0.25)
                end = min(after["start"] if after else w["end"], w["start"] + 0.9, nxt[0])
                if end - start >= 0.12:
                    found.append((start, end, w["w"]))
        for start, end, word in sorted(found):  # the same um heard by several passes: one cut
            if out and out[-1][0] <= start <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], end), "um", out[-1][3])
            else:
                out.append((start, end, "um", word))
        out.sort()
    if out:
        print(f"[{tag}] filler scan: {len(out)} ums cut at " + ", ".join(f"{x:.1f}s" for x, *_ in out))
    return out


def swap_words(segs: list, sw: dict, words_src: list, ranges: list, ed, speaker: str, tag: str) -> list:
    """`swap: {replace, with}`: cut the words `replace` (transcript text inside the short) and put in the
    same speaker saying `with` elsewhere in the panel (the nearest occurrence). Both edges of both
    stretches sit on the quietest 10 ms near the word boundaries; the picture jumps there like any cut."""
    hits = [h for lo, hi in ranges for h in find_in(words_src, sw["replace"], lo, hi)]
    if not hits:
        print(f"[{tag}] swap: can't find {sw['replace']!r} in the short, skipped")
        return segs
    r0, r1 = hits[0]
    donors = [h for h in find_in(words_src, sw["with"], 0, 1e9)
              if next(w for w in words_src if abs(w["start"] - h[0]) < 0.005)["speaker"] == speaker
              and not (r0 <= h[0] < r1)]
    if not donors:
        print(f"[{tag}] swap: {speaker} never says {sw['with']!r}, skipped")
        return segs
    d0, d1 = min(donors, key=lambda h: abs(h[0] - r0))
    nxt = lambda t: min((w["start"] for w in words_src if w["start"] >= t + 0.01 and w.get("speaker")), default=t)

    def quiet(t, lo, hi, floor=0.0):  # the quietest 10 ms in [t + lo, t + hi] (not before floor), source time
        i, j = int(max(t + lo, floor) * tighten.ENV_HZ), int((t + hi) * tighten.ENV_HZ)
        return (i + int(np.argmin(ed.env[i:j]))) / tighten.ENV_HZ if j > i else t

    # a stretch starts at the dip just before its first word and ends at the dip just before the next word
    c0, e0 = quiet(r0, -0.1, 0.15), quiet(d0, -0.1, 0.15)
    c1 = quiet(nxt(r1 - 0.01), -0.25, 0.05, floor=c0 + 0.2)
    e1 = quiet(nxt(d1 - 0.01), -0.25, 0.05, floor=e0 + 0.2)
    C0, C1, E0, E1 = (ed.to_out_near(x) for x in (c0, c1, e0, e1))
    out = []
    for sg in segs:
        x, y, kind = sg[:3]
        if not (x < C0 < y):
            out.append(sg)
            continue
        out += [(x, C0, kind, -1e9, r0), (E0, E1, kind, d0, nxt(d1 - 0.01)), (C1, y, kind, nxt(r1 - 0.01), 1e9)]
    print(f"[{tag}] swap: {sw['replace']!r} at {r0:.2f}s -> {sw['with']!r} from {d0:.2f}s")
    return out


# ---------------------------------------------------------------- plan

def plan(words: list, tl: Timeline, main: str, tracks: lf.Tracks, st: dict, reacts: list, hook_end: float) -> list:
    """Shots in short time: the speaker's close-up (anchored, i.e. tighter, through the hook),
    someone else when they talk a while (from l_cut into their first word), and the reactions."""
    def seen(name, a, b):
        return tracks.seen_share(name, tl.to_src(a), tl.to_src(b))

    fallback = main if tracks.has(main) else None
    trig = set(st["cutaway_triggers"])
    shots = []
    for spk, a, b in lf.turns(words, tl.dur, st["other_min"], st["l_cut"] if "speaker_change" in trig else 0.0):
        own = spk if spk and tracks.has(spk) and seen(spk, a, b) >= 0.5 else fallback
        shots.append({"kind": "close", "person": own, "speaker": spk, "t0": a, "t1": b})
    if hook_end > 0:  # the hook is the anchor sentence: its own (tighter) shot
        out = []
        for s in shots:
            if s["t0"] < hook_end < s["t1"]:
                out += [{**s, "t1": hook_end, "anchor": True}, {**s, "t0": hook_end}]
            else:
                out.append({**s, "anchor": True} if s["t1"] <= hook_end else s)
        shots = out
    for r0, r1, who in reacts:  # cut into whatever shot is there
        out = []
        for s in shots:
            if s["t1"] <= r0 or s["t0"] >= r1:
                out.append(s)
                continue
            if s["t0"] < r0:
                out.append({**s, "t1": r0})
            out.append({**s, "kind": "react", "person": who, "t0": max(s["t0"], r0), "t1": min(s["t1"], r1),
                        "anchor": False, "cutaway": True})
            if s["t1"] > r1:
                out.append({**s, "t0": r1})
        shots = out
    merged = []
    for s in shots:
        key = (s["kind"], s["person"], s.get("anchor", False))
        if merged and (merged[-1]["kind"], merged[-1]["person"], merged[-1].get("anchor", False)) == key:
            merged[-1]["t1"] = s["t1"]
        elif s["t1"] - s["t0"] > 0.05:
            merged.append(dict(s))
    return merged


class SmoothTracks(lf.Tracks):
    """Face tracks averaged over +-0.2 s, so a close-up or a pan never shakes with the detector."""

    def at(self, name, t: float) -> tuple:
        pts = [super(SmoothTracks, self).at(name, t + d) for d in (-0.2, -0.1, 0.0, 0.1, 0.2)]
        return tuple(float(np.mean([q[k] for q in pts])) for k in range(3))


class Mics:
    """The panel's mics as gain-corrected levels (dB; 0 = the mic's owner talking), and who held each
    mic around a given time (from the labelled words it carried; mics are passed around)."""

    def __init__(self, ev: dict, p: dict, words_src: list):
        import align
        s0, s1 = panel_range(ev, p)
        self.sr, self.ids = align.LABEL_SR, list(p["mics"])
        self.audio = [align.decode_timeline(ev, m, s0, s1 - s0, self.sr) for m in self.ids]
        self.live = [w for w in words_src if w.get("speaker") and not w.get("silent")]
        lv = np.array([[self._db(a, w["start"], max(w["end"], w["start"] + align.MIN_WINDOW)) for a in self.audio]
                       for w in self.live])
        self.gain = align.speech_gains(lv)
        self.word_mic = (lv + self.gain).argmax(axis=1)
        self.starts = np.array([w["start"] for w in self.live])

    def _db(self, x, t0, t1):
        a, b = max(0, int(t0 * self.sr)), min(len(x), int(t1 * self.sr))
        return 20 * np.log10(np.sqrt((x[a:b] ** 2).mean()) + 1e-6) if b > a else -120.0

    def level(self, t0: float, t1: float) -> np.ndarray:
        return np.array([self._db(a, t0, t1) for a in self.audio]) + self.gain

    def holder(self, m: int, t: float) -> str | None:
        on = np.nonzero(self.word_mic == m)[0]
        if not len(on):
            return None
        near = on[np.argsort(np.abs(self.starts[on] - t))[:25]]
        names = [self.live[i]["speaker"] for i in near]
        return max(set(names), key=names.count)


def audible_reactions(tl: Timeline, words: list, speaker: str, mics: Mics, st: dict) -> list:
    """[(S0, S1, person, why)] in short time: someone other than the speaker is heard. Their words in the
    transcript (a turn shorter than other_min; longer ones get their own shot), or their mic clearly
    louder than the speaker's for react_mic_min (a laugh, a "yeah"). Silence never counts."""
    out = []
    run = []
    for w in words + [None]:
        if w is not None and w["speaker"] != speaker:
            run.append(w)
            continue
        if run and run[-1]["end"] - run[0]["start"] < st["other_min"]:
            out.append((run[0]["start"], run[-1]["end"], run[0]["speaker"], f"says {' '.join(x['w'] for x in run)!r}"))
        run = []
    step = 0.25
    Ss = np.arange(0, tl.dur - step, step)
    L = np.array([mics.level(tl.to_src(S), tl.to_src(S) + step) for S in Ss])
    mine = [mics.word_mic[i] for i, w in enumerate(mics.live) if w["speaker"] == speaker
            and tl.to_src(0) - 30 <= w["start"] <= tl.to_src(tl.dur - 0.01) + 30]
    main = max(set(mine), key=mine.count) if mine else int(np.bincount(L.argmax(axis=1)).argmax())
    hit = [(L[k].argmax() != main and L[k].max() > st["react_mic_db"] and L[k].max() - L[k, main] >= 3.0) for k in range(len(Ss))]
    k = 0
    while k < len(Ss):
        if not hit[k]:
            k += 1
            continue
        j = k
        while j + 1 < len(Ss) and hit[j + 1] and L[j + 1].argmax() == L[k].argmax():
            j += 1
        if (j - k + 1) * step >= st["react_mic_min"]:
            m = int(L[k].argmax())
            who = mics.holder(m, tl.to_src(Ss[k]))
            if who and who != speaker:
                out.append((float(Ss[k]), float(Ss[j] + step), who, f"{mics.ids[m]} {L[k:j + 1, m].max():+.0f} dB"))
        k = j + 1
    return sorted(out)


def path_rect(points: list, u: float) -> tuple:
    """A point along the polyline through crop rects [(x0, y0, cw, ch)], u in [0, 1] of its length."""
    if len(points) == 1:
        return points[0]
    segl = [float(np.hypot(b[0] - a[0], b[1] - a[1])) for a, b in zip(points, points[1:])]
    total = sum(segl)
    if total < 1e-6:
        return points[-1]
    d = u * total
    for (a, b), l in zip(zip(points, points[1:]), segl):
        if d <= l or l == segl[-1] and (a, b) == (points[-2], points[-1]):
            f = 0 if l < 1e-6 else min(1.0, d / l)
            return tuple(a[i] + (b[i] - a[i]) * f for i in range(4))
        d -= l
    return points[-1]


def seat_rect(name: str, t_src: float, ch: float, tracks, size: tuple, st: dict, face=None) -> tuple:
    """A crop of height ch on one person, eyes on the upper third, kept inside the frame."""
    w, h = size
    cw = ch * st["size"][0] / st["size"][1]
    cx, cy, fh = face or tracks.at(name, t_src)
    x0 = min(max(0.0, cx - cw / 2), w - cw)
    y0 = min(max(0.0, cy - 0.12 * fh - st["eye_line"] * ch), h - ch)
    return (x0, y0, cw, ch)


def seats_between(seats: list, a: str, b: str, tracks, t0: float, t1: float) -> list:
    """The seated people from a to b (inclusive, in pan order), skipping anyone out of frame."""
    i, j = seats.index(a), seats.index(b)
    order = seats[i:j + 1] if i <= j else seats[j:i + 1][::-1]
    return [n for n in order if n in (a, b) or tracks.seen_share(n, t0, t1) >= 0.5]


def ease_fn(name: str):
    return lf.ease if name == "cubic" else (lambda u: min(1.0, max(0.0, u)))


def punches(shots: list, chops: list, bounds: list, st: dict) -> list:
    """Times the framing steps in or out within a shot: at jump cuts (it hides them), not too soon
    after the last change; and at a sentence boundary once one framing has held shot_max."""
    out, si, last = [], 0, 0.0
    marks = sorted([(c, "chop") for c in chops] + ([(b, "sentence") for b in bounds]
                                                   if "shot_max" in st["cutaway_triggers"] else []))
    for c, why in marks:
        while si + 1 < len(shots) and shots[si + 1]["t0"] <= c:
            si += 1
            last = shots[si]["t0"]
        if shots[si]["kind"] != "close" or shots[si]["t1"] - c < st["punch_min"]:
            continue
        if (why == "chop" and c - last >= st["punch_min"]) or (why == "sentence" and c - last >= st["shot_max"]):
            out.append(c)
            last = c
    return out


def rect(shot: dict, t_src: float, punched: bool, tracks: lf.Tracks, size: tuple, st: dict) -> tuple:
    """Portrait crop on cam2 (st["size"]): speaker ~2/3 of the frame, eyes on the upper third line; held still
    (tighter through the hook, never past max_zoom)."""
    w, h = size
    aspect = st["size"][0] / st["size"][1]
    if shot["person"] is None:
        ch = h
        cw = ch * aspect
        return ((w - cw) / 2, (h - ch) / 2, cw, ch)
    cx, cy, fh = tracks.at(shot["person"], t_src)
    mult = st["anchor_face_mult"] if shot.get("anchor") else st["close_face_mult"]
    ch = min(h, max(h / st["max_zoom"], fh * mult))
    if punched:
        ch = min(h, ch * st["punch"])
    cw = ch * aspect
    eyes = cy - 0.12 * fh
    x0 = min(max(0.0, cx - cw / 2), w - cw)
    y0 = min(max(0.0, eyes - st["eye_line"] * ch), h - ch)
    return (x0, y0, cw, ch)


# ---------------------------------------------------------------- anchor graphics

def face_boxes(r: tuple, t_src: float, tracks: lf.Tracks, st: dict) -> list:
    """Every tracked face visible at t_src, as output-frame boxes (head and hair, with a margin)."""
    x0, y0, cw, ch = r
    k = st["size"][0] / cw
    out = []
    for name in tracks.seats:
        if tracks.seen_share(name, t_src, t_src) < 0.3:
            continue
        cx, cy, fh = tracks.at(name, t_src)
        box = ((cx - 0.6 * fh - x0) * k, (cy - 0.85 * fh - y0) * k, (cx + 0.6 * fh - x0) * k, (cy + 0.9 * fh - y0) * k)
        if box[2] > 0 and box[0] < st["size"][0] and box[3] > 0 and box[1] < st["size"][1]:
            out.append(box)
    return out


def beats(spec: dict, words_src: list, lo: float, hi: float, ed, tl: Timeline, meat_at: float, st: dict, edir: Path,
          fps: float) -> list:
    """Each beat with an asset as {k, start, end, sticker} in short time; never under the wipe.
    A video asset plays from its `clip_in` for the beat's length; a still is a sticker."""
    out = []
    for k, beat in enumerate(spec.get("beats") or []):
        where = find_in(words_src, beat.get("at") or "", lo, hi)
        T = ed.to_out(where[0][0]) if where else None
        S = tl.to_S(T) if T is not None else None
        if S is None:
            print(f"[{spec['id']}] beat {k + 1}: can't find {beat.get('at')!r} in the short, skipped")
            continue
        if not beat.get("asset") or not (edir / beat["asset"]).exists():
            print(f"[{spec['id']}] beat {k + 1}: no asset yet (overlays.py fetch), skipped")
            continue
        start = max(S, 0.2)
        if abs(start - meat_at) < st["wipe_seconds"]:
            start = meat_at + st["wipe_seconds"] / 2
        length = min(max(float(beat.get("dur") or st["overlay_dur"]), 1.0), 3.0)
        end = min(start + length, tl.dur - 0.3)
        if end - start < 0.8:
            print(f"[{spec['id']}] beat {k + 1}: lands too late in the short, skipped")
            continue
        path = edir / beat["asset"]
        if path.suffix == ".mp4":
            width = int(st["size"][0] * min(max(float(beat.get("size") or st["overlay_video_width"]), 0.25), 0.45))
            sticker = ov.Clip(path, width, fps, end - start, float(beat.get("clip_in") or 0), beat.get("key") or "auto")
        else:
            width = int(st["size"][0] * min(max(float(beat.get("size") or st["overlay_width"]), 0.18), 0.22))
            sticker = ov.Sticker(path, width)
        out.append({"k": k, "start": start, "end": end, "sticker": sticker})
    out.sort(key=lambda x: x["start"])
    for x, y in zip(out, out[1:]):
        if y["start"] < x["end"]:
            print(f"[{spec['id']}] beats {x['k'] + 1} and {y['k'] + 1} overlap; the later one waits")
            y["start"] = x["end"]
    return [x for x in out if x["end"] - x["start"] >= 0.8]


# ---------------------------------------------------------------- overlays

class ClipReader:
    """Graded BGR frames of a b-roll clip: a portrait crop (centred at x_frac of the width) scaled to the
    short's size, read forward from `t_in` (seconds into the clip)."""

    def __init__(self, path: Path, t_in: float, dur: float, fps: float, size: tuple, x_frac: float, grade: str):
        w, h = probe(path)["width"], probe(path)["height"]
        ow, oh = size
        cw = min(w, int(round(h * ow / oh / 2)) * 2)
        x = int(min(max(0, x_frac * w - cw / 2), w - cw)) // 2 * 2
        vf = f"fps={fps},crop={cw}:{h}:{x}:0,scale={ow}:{oh}" + (f",{grade}" if grade else "")
        self.proc = subprocess.Popen(
            [str(Path(ffmpeg_with_libass())), "-nostdin", "-v", "error", "-ss", f"{t_in:.3f}", "-i", str(path),
             "-t", f"{dur + 1:.3f}", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.size, self.last = (ow, oh), np.zeros((oh, ow, 3), np.uint8)

    def next(self) -> np.ndarray:
        n = self.size[0] * self.size[1] * 3
        buf = self.proc.stdout.read(n)
        if len(buf) == n:
            self.last = np.frombuffer(buf, np.uint8).reshape(self.size[1], self.size[0], 3)
        return self.last

    def close(self):
        self.proc.stdout.close()
        self.proc.wait()


def audience_cutaways(spec: dict, p: dict, words: list, tl, dur: float, st: dict, quiet: list, tag: str,
                      n: int = 0) -> list:
    """[{t0, t1, clip, in, x}]: the room listening, cut in at sentence starts across the short. Clips come
    from event.yaml panel `audience` (b-roll filmed during this panel), each used once, rotated per short.
    `quiet`: [(S0, S1)] spans where no cutaway may land (the opening, the strap, graphics, reactions).
    `n`: the short's place in the story file, so neighbouring shorts start on different clips."""
    pool = p.get("audience") or []
    if spec.get("audience") is False or not pool or dur < st["audience_min_short"]:
        return []
    want = min(st["audience_max"], max(1, int(dur // st["audience_per"])))
    d = st["audience_seconds"]
    starts = [w["start"] for i, w in enumerate(words) if i and (lf.SENTENCE_END.search(words[i - 1]["w"])
                                                              or words[i - 1].get("eos"))
              and w["speaker"] == spec["speaker"] and words[i - 1]["speaker"] == spec["speaker"]]
    ok = [s for s in starts if s + d <= dur - st["audience_end_room"]
          and not any(a - 0.3 < s + d and s < b + 0.3 for a, b in quiet)]
    picks = []
    for frac in ([0.5] if want == 1 else [0.38, 0.72]):  # spread across the short
        free = [s for s in ok if all(abs(s - x) >= st["audience_gap"] for x in picks)]
        if free:
            picks.append(min(free, key=lambda s: abs(s - frac * dur)))
    from math import gcd
    step = next(s for s in range(st["audience_max"], st["audience_max"] + len(pool) + 1) if gcd(s, len(pool)) == 1)
    rot = n * step  # a step coprime with the pool size, so the shorts walk through every clip
    out = []
    for k, s in enumerate(sorted(picks)):
        c = pool[(rot + k) % len(pool)]
        out.append({"t0": round(s, 3), "t1": round(s + d, 3), "clip": c["clip"], "in": c["in"], "x": c.get("x", 0.5)})
    return out


# ---------------------------------------------------------------- render

def render(ev: dict, edir: Path, p: dict, spec: dict, plan_only: bool, replace: bool = False,
           clean: bool = False, thumb_at: float | None = None) -> Path:
    st = style()
    out_w, out_h = st["size"]
    fps = output_fps(ev)
    pstart, pend = panel_range(ev, p)
    tag = spec["id"]
    words_src = json.loads((edir / "transcripts" / f"{p['id']}.json").read_text())["words"]
    story = lf.load_story(edir / "longform" / f"{p['id']}.story.yaml")
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
        hooks = find_in(words_src, spec["hook"], 0, pend - pstart)
        if not hooks:
            print(f"[{tag}] SKIPPED: couldn't find the hook {spec['hook']!r}")
            return None
        ha, hb = min(hooks, key=lambda x: 0 if a <= x[0] <= b else min(abs(x[0] - a), abs(x[0] - b)))
    skips = [(x, y, "skip", "") for phrase in spec.get("skip") or [] for lo, hi in ranges
             for x, y in find_in(words_src, phrase, lo, hi)]
    cfg = tighten.settings("Short-form edit")
    if cfg.get("filler_scan"):
        skips += scan_fillers(edir, p["id"], ranges + ([(ha, hb)] if ha is not None else []), cfg, tag, words_src)
    editorial = tighten.resolve_cuts(words_src, story["cuts"]) + skips
    ed = tighten.Edit(words_src, tighten.decode_audio(edir / "transcripts" / f"{p['id']}.wav", tighten.SR),
                      pend - pstart, cfg, editorial, tighten.load_bed(edir, p["id"]))

    # the hook cold (frame 1 is its first word), then each part of the meat in order, without the hook
    segs = []
    if ha is not None:
        h0, h1 = padded(ed, words_src, ha, hb, st, pad_start=0.0)
        segs.append((h0, h1, "hook"))
    for k, (pa, pb) in enumerate(ranges):
        t0, t1 = padded(ed, words_src, pa, pb, st, pad_start=0.0 if not segs else None)
        if ha is not None and pa <= ha and hb <= pb and not spec.get("repeat_hook"):
            segs += [(x, y, "meat") for x, y in ((t0, ed.to_out_near(ha) - 0.02), (ed.to_out_near(hb) + 0.04, t1)) if y - x > 0.4]
        else:
            segs.append((t0, t1, "meat"))
    for sw in spec.get("swap") or []:  # say a word the speaker said elsewhere in place of one here
        segs = swap_words(segs, sw, words_src, ranges, ed, spec["speaker"], tag)
    tl = Timeline(segs, ed)
    dur = tl.dur
    meat_at = next((tl.offsets[k] for k, sg in enumerate(segs) if sg[2] != "hook"), dur) if ha is not None else 0.0
    words = tl.words(ed.words_out(words_src))
    for i, w in enumerate(words):  # a part can start mid-sentence: capitalise a caption that starts one
        if i == 0 or (words[i - 1]["seg"] != w["seg"] and (lf.SENTENCE_END.search(words[i - 1]["w"]) or words[i - 1].get("eos"))):
            words[i] = {**w, "w": w["w"][:1].upper() + w["w"][1:]}

    def phrase_S(phrase, lo, hi):  # short times of a transcript phrase inside source range [lo, hi]
        hits = find_in(words_src, phrase, lo, hi)
        if not hits:
            print(f"[{tag}] not found: {phrase!r}")
            return None
        A, B = ed.to_out(hits[0][0]), ed.to_out_near(hits[0][1])
        A = None if A is None else tl.to_S(A)
        return None if A is None else (A, tl.to_S(B) or A)

    chops = tl.chops()
    bounds = lf.boundaries(words, chops, st)
    tracks = SmoothTracks(edir / "faces" / f"{p['id']}.cam2.tracks.json")
    people = {x["name"]: x for x in p.get("people", []) if isinstance(x, dict)}
    size = tuple(probe(media(ev, synced(ev, "cam2")["file"]))[k] for k in ("width", "height"))
    hook_end = meat_at if ha is not None and len(segs) > 1 else 0.0
    end_room = st["other_min"]  # the short ends on the speaker, never on a reaction
    w_src, h_src = size
    seats = list(((p.get("seats") or {}).get("cam2")) or [])
    order = seats + [n for n in people if n not in seats]
    colors = {n: st["name_colors"][i % len(st["name_colors"])] for i, n in enumerate(order)}
    ease = ease_fn(st.get("sweep_ease", "cubic"))
    speaker = spec["speaker"]
    moves = []  # camera moves on cam2 that override the shot framing: {kind, t0, t1, ...}

    # --- the opening sweep: from the seat furthest from the speaker, past everyone in between, onto them
    sweep = None
    if st.get("sweep") and spec.get("sweep", True) and speaker in seats and tracks.has(speaker):
        # who's in frame over the opening, inside the first segment (a hook can sit after the meat in source)
        t0s, t1s = tl.to_src(0), tl.to_src(min(dur, 2.0, tl.offsets[1]) - 1e-3)
        seen = [n for n in seats if tracks.has(n) and tracks.seen_share(n, t0s, t1s) >= 0.5]
        start = spec.get("sweep_from") or max(
            (n for n in seen if n != speaker), key=lambda n: abs(seats.index(n) - seats.index(speaker)), default=None)
        key = phrase_S(spec["key"], min(ha if ha is not None else a, a), max(b, hb if hb is not None else b)) \
            if spec.get("key") else None
        land = st["sweep_seconds"] if key is None else max(0.6, min(st["sweep_seconds"], key[0] - 0.1))
        if start and start in seen:
            via = seats_between(seats, start, speaker, tracks, t0s, t1s)
            fixed = {n: tuple(np.median([tracks.at(n, tl.to_src(x)) for x in np.linspace(0, land, 7)], axis=0))
                     for n in via[:-1]}
            first = {"person": speaker, "anchor": hook_end > 0}
            normal = rect(first, tl.to_src(land), False, tracks, size, st)[3]
            ch = min(h_src, max(normal, h_src / st["max_zoom"]) * st["land_push"])
            sweep = {"kind": "sweep", "t0": 0.0, "t1": land, "via": via, "ch": ch, "fixed": fixed}
            moves.append(sweep)
            moves.append({"kind": "settle", "t0": land, "t1": land + st["land_settle"], "person": speaker, "ch": ch,
                          "end_ch": max(normal, h_src / st["max_zoom"])})
            others = [n for n in seen if n not in via]
            print(f"[{tag}] sweep 0.00-{land:.2f}s across {' -> '.join(via)}, lands on {speaker} at {land:.2f}s"
                  + (f", key words {spec['key']!r} at {key[0]:.2f}s" if key else "")
                  + (f"; not passed (other side of the speaker): {', '.join(others)}" if others else ""))
        else:
            print(f"[{tag}] sweep: nobody else in frame, skipped")
    land_at = moves[1]["t1"] if sweep else 0.0

    # --- reactions: only when someone else is heard (or hand-picked); a pan when they sit close, else a cut
    cands = []
    for r in spec.get("react") or []:  # hand-picked override
        rz = phrase_S(r["at"], a, b)
        if rz is not None:
            cands.append((rz[0], rz[0] + r.get("for", st["react_hold"][1]), r["who"], "hand-picked"))
    if spec.get("auto_react", True) and not spec.get("react") and "audible_reaction" in st["cutaway_triggers"]:
        cands += audible_reactions(tl, words, speaker, Mics(ev, p, words_src), st)
    reacts, pans = [], []
    pan_s, (hold_min, hold_max) = st["react_pan_seconds"], st["react_hold"]
    for r0, r1, who, why in sorted(cands):
        hold = min(hold_max, max(hold_min, r1 - r0))
        span = (r0, r0 + 2 * pan_s + hold)
        problem = ("in the anchor sentence" if r0 < hook_end else
                   f"within {st['react_after_land']:.0f}s of the sweep landing" if r0 < land_at + st["react_after_land"] else
                   "too close to the end (the short ends on the speaker)" if dur - span[1] < end_room else
                   f"{who} isn't seated on cam2" if who not in seats or not tracks.has(who) else
                   f"{who} is out of frame" if tracks.seen_share(who, tl.to_src(r0), tl.to_src(span[1] - 1e-3)) < 0.6 else
                   f"already {st['react_max']}" if len(reacts) + len(pans) >= st["react_max"] else
                   "overlaps the last one" if (pans or reacts) and r0 < max([x["end"] for x in pans] + [x[1] for x in reacts]) else None)
        if problem:
            print(f"[{tag}] reaction at {r0:.1f}s ({who}, {why}): skipped, {problem}")
            continue
        seats_away = abs(seats.index(who) - seats.index(speaker)) if speaker in seats else 99
        if seats_away > st["react_pan_max_seats"]:
            reacts.append((r0, r0 + hold, who))
            print(f"[{tag}] reaction cut at {r0:.2f}s to {who} ({seats_away} seats away, {why}), {hold:.1f}s")
            continue
        t0s, t1s = tl.to_src(r0), tl.to_src(span[1] - 1e-3)
        via = seats_between(seats, speaker, who, tracks, t0s, t1s)
        # seats passed in between hold still (their median face); both ends follow their tracked face
        fixed = {n: tuple(np.median([tracks.at(n, tl.to_src(x)) for x in np.linspace(*span, 7)], axis=0)) for n in via[1:-1]}
        ch = rect({"person": speaker}, t0s, False, tracks, size, st)[3]
        pans.append({"start": r0, "end": span[1], "who": who, "seats": via, "hold": hold, "why": why})
        moves += [{"kind": "pan", "t0": r0, "t1": r0 + pan_s, "via": via, "ch": ch, "fixed": fixed},
                  {"kind": "hold", "t0": r0 + pan_s, "t1": r0 + pan_s + hold, "person": who, "ch": ch},
                  {"kind": "pan", "t0": r0 + pan_s + hold, "t1": span[1], "via": via[::-1], "ch": ch, "fixed": fixed}]
        print(f"[{tag}] reaction pan at {r0:.2f}s: {' -> '.join(via)} ({why}), hold {hold:.1f}s, "
              f"back {r0 + pan_s + hold:.2f}-{span[1]:.2f}s")
    if spec.get("payoff"):  # the short must end on its payoff sentence
        pz = phrase_S(spec["payoff"], a, b)
        last = [c for c in bounds if c < dur - 0.3]
        if pz is not None and last and pz[0] < last[-1] - 0.05:
            print(f"[{tag}] WARNING: the payoff {spec['payoff']!r} is not in the last sentence")

    shots = plan(words, tl, speaker, tracks, st, reacts, hook_end)
    busy = [(m["t0"], m["t1"]) for m in moves]
    steps = [c for c in punches(shots, chops, bounds, st) if not any(x - 0.2 <= c <= y + 0.2 for x, y in busy)]
    shot_starts = [x["t0"] for x in shots]
    move_starts = [m["t0"] for m in moves]
    moves_sorted = sorted(moves, key=lambda m: m["t0"])

    def move_rect(m, S, t_src):
        u = (S - m["t0"]) / max(1e-6, m["t1"] - m["t0"])
        if m["kind"] in ("sweep", "pan"):
            pts = [seat_rect(n, t_src, m["ch"], tracks, size, st, m["fixed"].get(n)) for n in m["via"]]
            return path_rect(pts, ease(u))
        if m["kind"] == "settle":  # land: a slight push in, to the normal close-up size
            ch = m["ch"] + (m["end_ch"] - m["ch"]) * ease(u)
            return seat_rect(m["person"], t_src, max(ch, h_src / st["max_zoom"]), tracks, size, st)
        return seat_rect(m["person"], t_src, m["ch"], tracks, size, st)  # hold on the reactor

    def frame_rect(S, t_src):
        m = next((m for m in moves_sorted if m["t0"] <= S < m["t1"]), None)
        if m:
            return move_rect(m, S, t_src)
        shot = shots[max(0, bisect_right(shot_starts, S) - 1)]
        flips = bisect_right(steps, S) - bisect_right(steps, shot["t0"])
        return rect(shot, t_src, flips % 2 == 1, tracks, size, st)

    # the title: the headline on a frosted box at the top, from frame 1 to the end
    color_name = spec.get("color") or st["title_color"]
    if color_name not in st["summary_colors"]:
        print(f"[{tag}] WARNING: color {color_name!r} isn't one of the summary colours {st['summary_colors']}")
    short_color = gx.color_of(color_name, st)
    headline = spec.get("headline") or spec.get("anchor") or ""
    person = next((x for x in p.get("people", []) if isinstance(x, dict) and x["name"] == spec["speaker"]), {})
    byline = lf.clean(f"{person['role']} at {person['company']}") if person.get("role") and person.get("company") else ""
    title = gx.SummaryBand(headline, spec.get("title_key"), st, short_color, REPO / st["summary_logo"], tag,
                           lf.clean(spec["speaker"]), byline) if headline and not clean else None
    if headline and not spec.get("title_key"):
        print(f"[{tag}] note: no title_key, so nothing in the title is highlighted")

    # anchor graphics: kept off every face they'd cross during the beat, the title, the captions and the strap
    graphics = beats(spec, words_src, min(a, ha if ha is not None else a), max(b, hb if hb is not None else b),
                     ed, tl, meat_at, st, edir, fps) if st.get("overlays", True) and spec.get("overlays", True) else []
    zones = [(0, 0, out_w, (title.box[3] if title else 0) + 30),
             (0, out_h - st["caption_margin_v"] - st["caption_size"] - 40, out_w, out_h)]
    for g in graphics:
        beat = spec["beats"][g["k"]]
        sh, sw = g["sticker"].alpha.shape
        faces = []
        for S in np.arange(g["start"], g["end"], 1 / 8):
            t_src = tl.to_src(S)
            faces += face_boxes(frame_rect(S, t_src), t_src, tracks, st)
        if replace or not beat.get("pos"):
            beat["pos"] = list(ov.place(faces, sw + 2 * st["overlay_slide"], sh + 2 * st["overlay_slide"],
                                        (out_w, out_h), zones, st))
        cx, cy = beat["pos"][0] * out_w, beat["pos"][1] * out_h
        box = (cx - sw / 2, cy - sh / 2, cx + sw / 2, cy + sh / 2)
        if any(ov.overlap(box, f) for f in faces):
            print(f"[{tag}] beat {g['k'] + 1}: the graphic at {beat['pos']} crosses a face (--replace to move it)")
        g["xy"] = (cx, cy)

    # name straps: the speaker once the sweep has landed, and anyone else whose part starts a stretch
    def role(who):
        x = people.get(who) or {}
        return lf.clean(f"{x['role']} at {x['company']}" if x.get("role") and x.get("company") else x.get("company") or "")

    strap_at = [(land_at + st["strap_after_land"] if sweep else max(meat_at, 0.3), speaker)]
    for k, sg in enumerate(segs):
        first = next((w for w in words if w["seg"] == k), None)
        if sg[2] == "meat" and first and first["speaker"] not in (speaker, strap_at[-1][1]):
            strap_at.append((tl.offsets[k] + 0.2, first["speaker"]))
    straps = []
    for k, (s_in, who) in enumerate(strap_at if st.get("strap", True) else []):  # off: the summary names them
        s_out = min(s_in + st["strap_seconds"], dur - 0.5, (strap_at[k + 1][0] - st["strap_fade"]) if k + 1 < len(strap_at) else dur)
        if s_out - s_in < 1.0:
            continue
        layer = gx.FrostStrap(lf.clean(who), role(who), st, st["colors_darker"].get(color_name))  # under the captions, so the eye catches both
        layer.x, layer.y = (out_w - layer.w) // 2, out_h - st["caption_margin_v"] + st["strap_gap"]
        straps.append((s_in, s_out, layer, who))
    if any(s[2].y + s[2].h > out_h - st["progress_height"] - 10 for s in straps):
        print(f"[{tag}] WARNING: the name strap runs off the bottom (caption_margin_v too small)")

    # audience cutaways: the room listening, never over the opening, a strap, a graphic or a reaction
    quiet = [(0.0, max(hook_end, land_at + st["audience_after_land"]))] + \
            [(x[0], x[1] + st["strap_fade"]) for x in straps] + \
            [(g["start"] - 0.5, g["end"] + 0.5) for g in graphics] + \
            [(x["start"], x["end"]) for x in pans] + [(x[0], x[1]) for x in reacts]
    order = [s["id"] for s in (YAML(typ="safe").load((edir / "longform" / f"{p['id']}.story.yaml").read_text()) or {})
             .get("shorts") or [] if not s.get("rejected")]
    crowd = audience_cutaways(spec, p, words, tl, dur, st, quiet, tag, order.index(tag) if tag in order else 0)
    for c in crowd:
        print(f"[{tag}] audience cutaway {c['t0']:.2f}-{c['t1']:.2f}s: {c['clip']} from {c['in']:.1f}s")

    out_dir = edir / "shortform"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{tag}.plan.json").write_text(json.dumps(
        {"range_src": [a, b], "parts_src": ranges, "hook_src": [ha, hb], "segments": segs, "duration": round(dur, 2),
         "sweep": ({"t0": 0.0, "land": round(sweep["t1"], 3), "seats": sweep["via"], "crop_h": round(sweep["ch"], 1)}
                   if sweep else None),
         "reaction_pans": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in x.items()} for x in pans],
         "reaction_cuts": reacts, "moves": [{k: v for k, v in m.items() if k != "fixed"} for m in moves],
         "move_frames": [{"S": round(S, 3), "rect": [round(v, 1) for v in frame_rect(S, tl.to_src(S))],
                          "faces": {n: [round(v, 1) for v in tracks.at(n, tl.to_src(S))] for n in seats if tracks.has(n)
                                    and tracks.seen_share(n, tl.to_src(S), tl.to_src(S)) >= 0.5}}
                         for m in moves for S in np.arange(m["t0"], m["t1"], 1 / fps)],
         "punches": steps, "shots": shots, "audience": crowd,
         "title": {"text": headline, "box": list(title.box) if title else None, "color": short_color},
         "straps": [{"who": x[3], "in": round(x[0], 2), "out": round(x[1], 2), "box": [x[2].x, x[2].y, x[2].w, x[2].h]}
                    for x in straps],
         "graphics": [{"beat": g["k"] + 1, "start": round(g["start"], 3), "end": round(g["end"], 3),
                       "asset": spec["beats"][g["k"]]["asset"], "pos": spec["beats"][g["k"]]["pos"]} for g in graphics]},
        indent=1))
    # captions: no box, the word being said lit; someone else's first line says who, in their colour
    lines = gx.caption_line_groups(words, st)
    prefixes, prev = [], speaker
    for ln in lines:
        who = ln[0]["speaker"]
        prefixes.append(f"{{\\c{lf.ass_color(colors.get(who, st['name_colors'][0]))}}}{lf.clean(who.split()[0])}: "
                        f"{{\\c{lf.ass_color('#FFFFFF')}}}" if who != speaker and who != prev else "")
        prev = who
    hi = gx.color_of(ev.get("caption_highlight") or st["caption_highlight"], st)
    ass = out_dir / f"{tag}.ass"
    gx.write_captions(ass, lines, st, dur, hi, [st["caption_margin_v"]] * len(lines), prefixes)
    kinds = {}
    for s in shots:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    raw = sum(y - x for x, y in ranges) + ((hb - ha) if ha is not None and not any(x <= ha < y for x, y in ranges) else 0)
    hook_s = f"hook {segs[0][1] - segs[0][0]:.1f}s" if ha is not None else "no hook"
    print(f"[{tag}] {dur:.1f}s ({raw:.1f}s before tightening; {hook_s}, {len(ranges)} parts), {len(shots)} shots "
          f"({', '.join(f'{v} {k}' for k, v in kinds.items())}), {len(chops)} jump cuts, {len(steps)} punches, "
          f"{len(pans)} reaction pans, {len(reacts)} reaction cuts, {len(graphics)} graphics")
    if dur > st["max_seconds"] + 0.05:
        print(f"[{tag}] FAIL: {dur:.1f}s is over the {st['max_seconds']}s limit: tighten the range or reject it "
              f"(`rejected: too long for {st['max_seconds']}s`). Not rendered.")
        return None
    lo_t, hi_t = st["target_seconds"]
    if not lo_t <= dur <= hi_t:
        print(f"[{tag}] note: {dur:.1f}s is outside the {lo_t}-{hi_t}s target")
    if plan_only:
        return out_dir / f"{tag}.plan.json"

    if thumb_at is not None:  # one frame for the post's thumbnail: the band and the speaker, no captions
        S = min(max(0.0, thumb_at), dur - 1 / fps)
        if any(c["t0"] - 0.2 <= S < c["t1"] + 0.2 for c in crowd):
            print(f"[{tag}] WARNING: the thumbnail at {S:.1f}s is in an audience cutaway; pick another `thumb_at`")
        t_src = tl.to_src(S)
        dec = lf.Decoder(ev, "cam2", pstart + t_src, 0.5, fps, size, st.get("grade") or "")
        frame = lf.warp(dec.at(pstart + t_src), frame_rect(S, t_src), out_w, out_h)
        dec.close()
        if title:
            title.draw(frame, S)
        post_dir = edir / "posts"
        post_dir.mkdir(exist_ok=True)
        out = post_dir / f"{tag}.jpg"
        cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"[{tag}] thumbnail at {S:.2f}s -> {out.relative_to(edir)}")
        return out

    with tempfile.TemporaryDirectory() as tmp:
        full = ed.audio()
        audio = tighten.splice(full, [(int(sg[0] * tighten.SR), int(sg[1] * tighten.SR)) for sg in segs])
        head, tail = int(0.005 * tighten.SR), int(0.06 * tighten.SR)  # frame 1 is a word: no slow fade in
        audio[:head] *= np.linspace(0, 1, head, dtype=np.float32)
        audio[-tail:] *= np.linspace(1, 0, tail, dtype=np.float32)
        wav = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(tighten.SR), "-ac", "1",
                        "-i", "-", "-c:a", "pcm_f32le", str(wav)], input=audio.astype(np.float32).tobytes(), check=True)
        # clean: no text burnt in at all (no captions, name tags or anchor card), for captioning elsewhere
        out = out_dir / (f"{tag}.clean.mp4" if clean else f"{tag}.mp4")
        versions = [(None if clean else ass, out)]
        encs = [subprocess.Popen(
            [ffmpeg_with_libass(), "-nostdin", "-v", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "-",
             "-i", str(wav), "-filter_complex",
             f"[0:v]subtitles=filename='{str(sub).replace(':', chr(92) + ':')}'[v]" if sub else "[0:v]null[v]",
             "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(dst)],
            stdin=subprocess.PIPE) for sub, dst in versions]
        frames = int(round(dur * fps))
        dec, dec_seg = None, -1
        began = time.time()
        try:
            for n in range(frames):
                S = n / fps
                k = tl.seg_at(S)
                if k != dec_seg:  # each segment reads its own stretch of cam2
                    if dec:
                        dec.close()
                    src0, src1 = ed.to_src(segs[k][0]), ed.to_src(segs[k][1] - 1e-3)
                    dec = lf.Decoder(ev, "cam2", pstart + src0, src1 - src0, fps, size, st.get("grade") or "")
                    dec_seg = k
                t_src = tl.to_src(S)
                src = dec.at(pstart + t_src)  # keep reading cam2 through a cutaway, so it stays in sync
                cut = next((c for c in crowd if c["t0"] <= S < c["t1"]), None)
                if cut:
                    if cut.get("reader") is None:
                        cut["reader"] = ClipReader(media(ev, cut["clip"]), cut["in"], cut["t1"] - cut["t0"], fps,
                                                   (out_w, out_h), cut["x"], st.get("grade") or "")
                    frame = cut["reader"].next().copy()
                else:
                    frame = lf.warp(src, frame_rect(S, t_src), out_w, out_h)
                for g in graphics:  # the overlay track, above the footage and under the captions
                    opacity, dy = ov.motion(S - g["start"], g["end"] - g["start"], st)
                    g["sticker"].draw(frame, g["xy"][0], g["xy"][1] + dy, opacity, S - g["start"])
                if st["wipe_seconds"] and len(segs) > 1 and abs(S - meat_at) < st["wipe_seconds"] / 2:
                    lf.draw_wipe(frame, 0.5 + (S - meat_at) / st["wipe_seconds"], st["wipe_colors"])
                if title:
                    title.draw(frame, S)
                for s_in, s_out, layer, _ in straps if not clean else []:
                    gx.draw_strap(frame, layer, S, s_in, s_out, st)
                if st.get("progress_height"):
                    lf.draw_progress(frame, S / dur, st)
                for enc in encs:
                    enc.stdin.write(frame.tobytes())
        finally:
            for enc in encs:
                enc.stdin.close()
                enc.wait()
            if dec:
                dec.close()
            for c in crowd:
                if c.get("reader"):
                    c["reader"].close()
    if any(enc.returncode for enc in encs):
        sys.exit(f"ffmpeg failed ({[enc.returncode for enc in encs]})")
    print(f"[{tag}] rendered in {time.time() - began:.0f}s -> " + ", ".join(str(d.relative_to(edir)) for _, d in versions))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("panel")
    ap.add_argument("--id", action="append", help="only these shorts (repeatable)")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--replace", action="store_true", help="place every anchor graphic again")
    ap.add_argument("--clean", action="store_true",
                    help="render <id>.clean.mp4 with no text on screen (no captions, name tags or anchor card)")
    ap.add_argument("--thumb", action="store_true",
                    help="write only the post thumbnail, events/<event>/posts/<id>.jpg (spec post.thumb_at, default thumb_at)")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    p = panel(ev, args.panel)
    story = YAML(typ="safe").load((edir / "longform" / f"{p['id']}.story.yaml").read_text()) or {}
    specs = [s for s in story.get("shorts") or [] if (s["id"] in args.id if args.id else s.get("hook") or s.get("parts"))]
    if not specs:
        sys.exit("no shorts with a hook in the story file (or none match --id)")
    for spec in specs:
        thumb = ((spec.get("post") or {}).get("thumb_at", style()["thumb_at"])) if args.thumb else None
        render(ev, edir, p, spec, args.plan_only, args.replace, args.clean, thumb)
    save_positions(edir / "longform" / f"{p['id']}.story.yaml", specs)


def save_positions(path: Path, specs: list) -> None:
    """Record where each anchor graphic went in the story file (comments and order kept)."""
    story = ov.story_yaml.load(path.read_text())
    placed = {s["id"]: s.get("beats") or [] for s in specs}
    changed = False
    for spec in story.get("shorts") or []:
        for beat, done in zip(spec.get("beats") or [], placed.get(spec["id"], [])):
            if done.get("pos") and list(beat.get("pos") or []) != done["pos"]:
                beat["pos"] = CommentedSeq(done["pos"])
                beat["pos"].fa.set_flow_style()
                changed = True
    if changed:
        ov.save_story(path, story)


if __name__ == "__main__":
    main()
