"""Tighten a panel's speech: mute ums, cut long pauses, long ums and stutters.

    .venv/bin/python scripts/edit.py <event> <panel>     # report what would be cut and muted

ShortsHog's core tenet is dynamic output, and dead air is the opposite. From the
labelled transcript and the panel's mic mix this builds an edit decision list in
panel time (settings: "## Dynamic edit" in FORMAT.md):

  mute   every filler: transcribed ones ("um", "uh", ...) and the ones whisper left
         out, found as a short pause with voice in it but no words
  cut    pauses longer than pause_max (shortened to pause_keep), fillers longer
         than filler_cut, stutters (a word or two-word phrase said twice in a row:
         "we we merge", "you want to you want to") and leading/trailing dead air;
         plus editorial cuts from the panel's story file (mic checks, setup chatter,
         asides that don't serve the story)
Cut edges are nudged to the quietest 10 ms nearby so no word is clipped. Every audio cut is a
15 ms equal-power crossfade, and muted spans are filled with the panel's own room tone (its
quietest 2 s with no speech), never digital silence.
Shorts use the harder "## Short-form edit" profile on top (`settings("Short-form edit")`):
every filler cut, pauses down to a breath, restarts up to 4 words, standalone "you know" / "right?".

`Edit` maps between source (panel) time and output time, and gives the audio with
cuts crossfaded and mutes filled with room tone. `splice()` does the same crossfade for any
list of ranges (the long form's cold open, a short's hook and meat).
"""
import argparse
import json
import re
import sys
from bisect import bisect_right
from pathlib import Path

import numpy as np
from ruamel.yaml import YAML

from common import REPO, decode_audio, event_dir, load_event, panel

SR = 48000
ENV_HZ = 100
XFADE = 0.015       # s, equal-power crossfade at each cut and at each edge of a muted span
TONE = 2.0          # s of room tone sampled per panel
SNAP = 0.08         # s, search for a quiet point either side of a cut edge
MIN_CUT = 0.12      # s, shorter cuts aren't worth a jump
MIN_KEEP = 0.25     # s, shorter kept slivers are folded into the cut around them
KEEP_DOUBLES = {"no", "yeah", "very", "really", "so", "ha", "bye", "check", "go", "okay", "ok", "right", "well"}


def settings(profile: str = "Dynamic edit") -> dict:
    """A FORMAT.md edit block; any profile other than "Dynamic edit" overrides its values."""
    text = (REPO / "FORMAT.md").read_text()

    def block(name):
        m = re.search(rf"## {name}\n.*?```yaml\n(.*?)```", text, re.S)
        if not m:
            sys.exit(f"FORMAT.md needs a ```yaml block under '## {name}'")
        return YAML(typ="safe").load(m.group(1))
    base = block("Dynamic edit")
    return base if profile == "Dynamic edit" else {**base, **block(profile)}


def norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def merge(spans: list) -> list:
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1e-3:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def end_before_next(live: list, i: int) -> float:
    """End of live[i], but not past the next word's start (whisper's word ends can overlap)."""
    return min(live[i]["end"], live[i + 1]["start"]) if i + 1 < len(live) else live[i]["end"]


def resolve_cuts(words: list, specs: list) -> list:
    """Editorial cuts given as transcript text -> [(start, end, why, text)] in panel time.

    A spec is {from, to, why}: from the first word of `from` to the last word of the first
    `to` found after it (within two minutes). Unmatched specs are reported, not guessed.
    """
    live = [w for w in words if not w.get("silent") and w.get("speaker")]
    toks = [norm(w["w"]) for w in live]

    def find(phrase, start=0):
        q = [t for t in (norm(x) for x in phrase.split()) if t]
        for i in range(start, len(toks) - len(q) + 1):
            j, k = i, 0
            while j < len(toks) and k < len(q):
                if not toks[j]:
                    j += 1
                    continue
                if toks[j] != q[k]:
                    break
                j, k = j + 1, k + 1
            if k == len(q):
                return i, j - 1
        return None

    out = []
    for spec in specs or []:
        a = find(spec["from"])
        if a is None:
            print(f"  cut not found: from {spec['from']!r}")
            continue
        b = find(spec["to"], a[0])
        if b is None or live[b[1]]["end"] - live[a[0]]["start"] > 120:
            print(f"  cut not found: to {spec['to']!r} after {spec['from']!r}")
            continue
        text = " ".join(w["w"] for w in live[a[0]:b[1] + 1])
        out.append((live[a[0]]["start"], end_before_next(live, b[1]), spec.get("why", ""), text))
    return out


class Edit:
    def __init__(self, words: list, pcm: np.ndarray, dur: float, cfg: dict, editorial: list = (),
                 bed: np.ndarray | None = None):
        """pcm: the mic sum (timing and the envelope); bed: what audio() plays, if given (mix.py's
        single-open-mic render bed, without the sum's echo)."""
        self.dur = dur
        self.cfg = cfg
        self.pcm = pcm
        self.bed = bed[:len(pcm)] if bed is not None and len(bed) >= len(pcm) - SR else None
        hop = SR // ENV_HZ
        frames = len(pcm) // hop
        self.env = 10 * np.log10((pcm[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1) + 1e-10)
        speech_db = np.percentile(self.env, 90)
        fillers = set(cfg["fillers"])

        live = [w for w in words if not w.get("silent") and w.get("speaker")]
        cuts, mutes, dropped = [], [], set()
        self.editorial = list(editorial)
        for a, b, *_ in self.editorial:  # cut with everything said inside
            cuts.append((a, b))
            dropped.update(id(w) for w in live if a - 1e-3 <= w["start"] and w["end"] <= b + 1e-3)
        # transcribed fillers
        for w in live:
            if norm(w["w"]) in fillers:
                (cuts if w["end"] - w["start"] >= cfg["filler_cut"] else mutes).append((w["start"], w["end"]))
                dropped.add(id(w))
        # tag fillers ("you know", "right?", "I mean"): cut where they stand alone between
        # punctuation or pauses, never mid-sentence
        tags = [[norm(x) for x in t.split()] for t in cfg.get("tag_fillers") or []]
        for i in range(len(live)):
            for q in tags:
                j = i + len(q) - 1
                if j >= len(live) or [norm(w["w"]) for w in live[i:j + 1]] != q:
                    continue
                if any(id(w) in dropped for w in live[i:j + 1]):
                    continue
                before = i == 0 or re.search(r"[,.?!]$", live[i - 1]["w"]) or live[i]["start"] - live[i - 1]["end"] > 0.15
                after = re.search(r"[,.?!]$", live[j]["w"]) or j + 1 == len(live) or live[j + 1]["start"] - live[j]["end"] > 0.15
                if before and after and len({w["speaker"] for w in live[i:j + 1]}) == 1:
                    cuts.append((live[i]["start"], end_before_next(live, j)))
                    dropped.update(id(w) for w in live[i:j + 1])
        # stutters and restarts: a phrase of up to repeat_max words said twice in a row by the
        # same person ("we we merge", "you want to you want to"); the first go is cut
        # (a filler between the two goes doesn't count: "that uh that" is a stutter too)
        n_max = cfg.get("repeat_max", 2)
        seq = [w for w in live if id(w) not in dropped]
        for i in range(len(seq) - 1):
            if id(seq[i]) in dropped:
                continue
            for n in range(1, n_max + 1):
                if i + 2 * n > len(seq):
                    break
                first, second = seq[i:i + n], seq[i + n:i + 2 * n]
                q = [norm(w["w"]) for w in first]
                pause = second[0]["start"] - first[-1]["end"] - sum(
                    w["end"] - w["start"] for w in live if id(w) in dropped and first[-1]["end"] <= w["start"] < second[0]["start"])
                if (not all(q) or q != [norm(w["w"]) for w in second] or (n == 1 and q[0] in KEEP_DOUBLES)
                        or len({w["speaker"] for w in first + second}) != 1
                        or pause >= 0.6
                        or any(id(w) in dropped for w in first + second)):
                    continue
                cuts.append((first[0]["start"], second[0]["start"]))
                dropped.update(id(w) for w in first)
                break
        # pauses, and ums whisper didn't write down (voice in a pause, no words)
        kept = [w for w in live if id(w) not in dropped]
        edges = [(0.0, kept[0]["start"] if kept else dur)] + \
                [(a["end"], b["start"]) for a, b in zip(kept, kept[1:])] + \
                [(kept[-1]["end"] if kept else 0.0, dur)]
        half = cfg["pause_keep"] / 2
        for k, (a, b) in enumerate(edges):
            gap = b - a
            lead_or_tail = k == 0 or k == len(edges) - 1
            if gap > cfg["pause_max"] or (lead_or_tail and gap > cfg["pause_keep"]):
                lo = a if k == 0 else a + half
                hi = b if k == len(edges) - 1 else b - half
                if not lead_or_tail:  # whisper's word times can be off: only cut what is actually quiet
                    lo, hi = self._quiet_run(lo, hi, speech_db - cfg.get("pause_quiet_db", 10))
                if hi - lo > 0:
                    cuts.append((lo, hi))
            elif gap >= cfg["um_min"]:
                seg = self.env[int(a * ENV_HZ):int(b * ENV_HZ)]
                if len(seg) and np.mean(seg > speech_db - cfg["um_voice_db"]) > 0.5:
                    (cuts if cfg.get("um_cut") else mutes).append((a + 0.03, b - 0.03))
        # snap cut edges to a quiet point, but never into a word that stays
        keep_words = sorted((w["start"], w["end"]) for w in live if id(w) not in dropped)
        starts = [a for a, _ in keep_words]
        snapped = []
        for a, b in merge(cuts):
            i = bisect_right(starts, a + 0.05)
            left = max([e for _, e in keep_words[max(0, i - 3):i] if e <= a + 0.05] or [0.0])
            j = bisect_right(starts, b - 0.05)
            right = keep_words[j][0] if j < len(keep_words) else dur
            sa, sb = self._snap(a, b)
            snapped.append((min(max(sa, left), b), max(min(sb, right), a)))
        cuts = snapped
        cuts = [(a, b) for a, b in merge(cuts) if b - a >= MIN_CUT]
        keeps, t = [], 0.0
        for a, b in cuts:
            if a - t >= MIN_KEEP:
                keeps.append((t, a))
            t = b
        if dur - t >= MIN_KEEP:
            keeps.append((t, dur))
        self.keeps = keeps
        self.mutes = merge([(a, b) for a, b in mutes if b > a])
        self.live = live
        self.tone_at = None
        self.dropped = dropped
        self.out_starts = np.cumsum([0.0] + [b - a for a, b in keeps[:-1]]).tolist()
        self.out_dur = sum(b - a for a, b in keeps)

    def _quiet_run(self, lo: float, hi: float, db: float) -> tuple:
        """The longest stretch of [lo, hi] below db (envelope frames), or (lo, lo) if there is none."""
        i0, i1 = int(lo * ENV_HZ), int(hi * ENV_HZ)
        best, start = (lo, lo), None
        for i in range(i0, i1 + 1):
            quiet = i < i1 and i < len(self.env) and self.env[i] < db
            if quiet and start is None:
                start = i
            elif not quiet and start is not None:
                if (i - start) / ENV_HZ > best[1] - best[0]:
                    best = (start / ENV_HZ, i / ENV_HZ)
                start = None
        return best

    def _snap(self, a: float, b: float) -> tuple:
        def quiet(t):
            lo, hi = int((t - SNAP) * ENV_HZ), int((t + SNAP) * ENV_HZ)
            lo, hi = max(0, lo), min(len(self.env), hi)
            return (lo + int(np.argmin(self.env[lo:hi]))) / ENV_HZ if hi > lo else t
        return (quiet(a) if a > 0 else a, quiet(b) if b < self.dur else b)

    # ---- time mapping
    def to_out(self, t: float) -> float | None:
        """Output time of source time t, or None if t was cut."""
        k = bisect_right([a for a, _ in self.keeps], t) - 1
        if k < 0 or t > self.keeps[k][1]:
            return None
        return self.out_starts[k] + (t - self.keeps[k][0])

    def to_out_near(self, t: float) -> float:
        """Output time of source time t, or of the next kept moment if t was cut."""
        k = bisect_right([a for a, _ in self.keeps], t) - 1
        if k >= 0 and t <= self.keeps[k][1]:
            return self.out_starts[k] + (t - self.keeps[k][0])
        k += 1
        return self.out_starts[k] if k < len(self.keeps) else self.out_dur

    def to_src(self, T: float) -> float:
        k = max(0, bisect_right(self.out_starts, T) - 1)
        return self.keeps[k][0] + (T - self.out_starts[k])

    def chops(self) -> list:
        """Output times where a cut joins two kept stretches."""
        return self.out_starts[1:]

    def words_out(self, words: list) -> list:
        """Kept words on the output timeline (fillers and stutters left out). A kept word gets `eos` when
        the sentence ended on a word that was cut after it ("... for that, right?" with "right?" cut)."""
        out = []
        for w in words:
            if w.get("silent") or not w.get("speaker"):
                continue
            if id(w) in self.dropped or self.to_out(w["start"]) is None:
                if out and re.search(r"[.?!]['\")]*$", w["w"]) and w["speaker"] == out[-1]["speaker"]:
                    out[-1]["eos"] = True
                continue
            a, b = self.to_out(w["start"]), self.to_out(min(w["end"], w["start"] + 0.05))
            end = self.to_out(w["end"])
            out.append({**w, "start": round(a, 3), "end": round(end if end is not None else a + (w["end"] - w["start"]), 3),
                        "src": [w["start"], w["end"]]})
        return out

    # ---- audio
    def room_tone(self) -> np.ndarray:
        """The panel's quietest TONE seconds with no speech (no word within 0.2 s), skipping digital silence."""
        hop, n = SR // ENV_HZ, int(TONE * ENV_HZ)
        bad = self.env < -90
        for w in self.live:
            bad[max(0, int((w["start"] - 0.2) * ENV_HZ)):int((w["end"] + 0.2) * ENV_HZ)] = True
        if len(self.env) <= n:
            return np.zeros(int(TONE * SR), np.float32)
        cb = np.concatenate([[0], np.cumsum(bad)])
        cp = np.concatenate([[0.0], np.cumsum(10 ** (self.env / 10))])
        starts = np.arange(len(self.env) - n)
        level = (cp[starts + n] - cp[starts]) / n
        ok = cb[starts + n] == cb[starts]
        k = int(starts[ok][np.argmin(level[ok])] if ok.any() else starts[np.argmin(level)])
        self.tone_at = k / ENV_HZ
        return self.source()[k * hop:(k + n) * hop].astype(np.float32)

    def source(self) -> np.ndarray:
        return self.bed if self.bed is not None else self.pcm

    def audio(self) -> np.ndarray:
        x = self.source().astype(np.float32).copy()
        if len(x) < len(self.pcm):
            x = np.concatenate([x, np.zeros(len(self.pcm) - len(x), np.float32)])
        r = int(XFADE * SR)
        tone = self.room_tone() if self.mutes else None
        for a, b in self.mutes:  # room tone in place of the um, crossfaded in and out
            i, j = int(a * SR), int(b * SR)
            if j - i <= 2 * r:
                continue
            fill = loop(tone, (j - i) + i % len(tone), r)[i % len(tone):]
            gain = np.ones(j - i, np.float32)
            th = np.linspace(0, np.pi / 2, r, dtype=np.float32)
            gain[:r], gain[-r:] = np.sin(th), np.cos(th)
            x[i:j] = x[i:j] * np.sqrt(np.clip(1 - gain ** 2, 0, 1)) + fill * gain
        return splice(x, [(int(a * SR), int(b * SR)) for a, b in self.keeps])

    def summary(self) -> str:
        muted = sum(b - a for a, b in self.mutes)
        ed = sum(b - a for a, b, *_ in self.editorial)
        return (f"{self.dur / 60:.1f} -> {self.out_dur / 60:.1f} min ({self.dur - self.out_dur:.0f}s cut in "
                f"{len(self.keeps) - 1} places, {len(self.editorial)} editorial cuts = {ed:.0f}s), "
                f"{len(self.mutes)} ums muted ({muted:.0f}s)")


def _take(x: np.ndarray, i: int, j: int) -> np.ndarray:
    """x[i:j], zero-padded where it runs off either end."""
    out = np.zeros(j - i, np.float32)
    lo, hi = max(0, i), min(len(x), j)
    if hi > lo:
        out[lo - i:hi - i] = x[lo:hi]
    return out


def splice(x: np.ndarray, ranges: list, xfade: float = XFADE) -> np.ndarray:
    """x[a:b] for each (a, b) sample range, joined end to end with an equal-power crossfade centred on
    each join. The fade borrows xfade/2 of audio past each edge, so the output length is exactly the
    sum of the ranges and the time mapping is unchanged."""
    out = np.concatenate([x[a:b] for a, b in ranges]).astype(np.float32) if ranges else x[:0].astype(np.float32)
    h = int(xfade * SR) // 2
    th = np.linspace(0, np.pi / 2, 2 * h, dtype=np.float32)
    pos = 0
    for (a, b), (na, nb) in zip(ranges, ranges[1:]):
        pos += b - a
        if h and b - a >= 2 * h and nb - na >= 2 * h:
            out[pos - h:pos + h] = _take(x, b - h, b + h) * np.cos(th) + _take(x, na - h, na + h) * np.sin(th)
    return out


def loop(tone: np.ndarray, n: int, r: int) -> np.ndarray:
    """n samples of tone repeated, each repeat crossfaded into the next over r samples."""
    out = tone.astype(np.float32).copy()
    th = np.linspace(0, np.pi / 2, r, dtype=np.float32)
    while len(out) < n:
        nxt = tone.astype(np.float32).copy()
        nxt[:r] = out[-r:] * np.cos(th) + nxt[:r] * np.sin(th)
        out = np.concatenate([out[:-r], nxt])
    return out[:n]


def load_bed(edir: Path, panel_id: str) -> np.ndarray | None:
    """mix.py's render bed for a panel, or None (then renders play the mic sum)."""
    path = edir / "transcripts" / f"{panel_id}.bed.wav"
    if not path.exists():
        print(f"  no {path.name}: renders use the mic sum, which echoes (run mix.py)")
        return None
    return decode_audio(path, SR)


def build(edir: Path, p: dict, words: list, dur: float, cut_specs: list = ()) -> Edit:
    pcm = decode_audio(edir / "transcripts" / f"{p['id']}.wav", SR)
    return Edit(words, pcm, dur, settings(), resolve_cuts(words, cut_specs), load_bed(edir, p["id"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("panel")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    p = panel(ev, args.panel)
    words = json.loads((edir / "transcripts" / f"{p['id']}.json").read_text())["words"]
    from common import panel_range
    a, b = panel_range(ev, p)
    story = edir / "longform" / f"{p['id']}.story.yaml"
    specs = (YAML(typ="safe").load(story.read_text()) or {}).get("cuts") if story.exists() else []
    e = build(edir, p, words, b - a, specs or [])
    for s0, s1, why, text in e.editorial:
        print(f"  cut {int(s0 // 60)}:{s0 % 60:04.1f}-{int(s1 // 60)}:{s1 % 60:04.1f} ({why}): {text}")
    print(f"[{p['id']}] {e.summary()}")


if __name__ == "__main__":
    main()
