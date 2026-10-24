"""Tighten a panel's speech: mute ums, cut long pauses, long ums and stutters.

    .venv/bin/python scripts/edit.py <event> <panel>     # report what would be cut and muted

ShortsHog's core tenet is dynamic output, and dead air is the opposite. From the
labelled transcript and the panel's mic mix this builds an edit decision list in
panel time (settings: "## Dynamic edit" in FORMAT.md):

  mute   every filler: transcribed ones ("um", "uh", ...) and the ones whisper left
         out, found as a short pause with voice in it but no words
  cut    pauses longer than pause_max (shortened to pause_keep), fillers longer
         than filler_cut, stutters (a word or two-word phrase said twice in a row:
         "we we merge", "you want to you want to") and leading/trailing dead air
Cut edges are nudged to the quietest 10 ms nearby so no word is clipped.

`Edit` maps between source (panel) time and output time, and gives the audio with
cuts joined (short fades) and mutes applied.
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
FADE = 0.008        # s, crossfade at each cut and ramp at each mute
SNAP = 0.08         # s, search for a quiet point either side of a cut edge
MIN_CUT = 0.12      # s, shorter cuts aren't worth a jump
MIN_KEEP = 0.25     # s, shorter kept slivers are folded into the cut around them
KEEP_DOUBLES = {"no", "yeah", "very", "really", "so", "ha", "bye", "check", "go", "okay", "ok", "right", "well"}


def settings() -> dict:
    text = (REPO / "FORMAT.md").read_text()
    m = re.search(r"## Dynamic edit.*?```yaml\n(.*?)```", text, re.S)
    if not m:
        sys.exit("FORMAT.md needs a ```yaml block under '## Dynamic edit'")
    return YAML(typ="safe").load(m.group(1))


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


class Edit:
    def __init__(self, words: list, pcm: np.ndarray, dur: float, cfg: dict):
        self.dur = dur
        self.cfg = cfg
        self.pcm = pcm
        hop = SR // ENV_HZ
        frames = len(pcm) // hop
        self.env = 10 * np.log10((pcm[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1) + 1e-10)
        speech_db = np.percentile(self.env, 90)
        fillers = set(cfg["fillers"])

        live = [w for w in words if not w.get("silent") and w.get("speaker")]
        cuts, mutes, dropped = [], [], set()
        # transcribed fillers
        for w in live:
            if norm(w["w"]) in fillers:
                (cuts if w["end"] - w["start"] >= cfg["filler_cut"] else mutes).append((w["start"], w["end"]))
                dropped.add(id(w))
        # stutters: a word, or a two-word phrase, said twice in a row by the same person
        for i in range(len(live) - 1):
            a, b = live[i], live[i + 1]
            if id(a) in dropped:
                continue
            if (norm(a["w"]) and norm(a["w"]) == norm(b["w"]) and norm(a["w"]) not in KEEP_DOUBLES
                    and a["speaker"] == b["speaker"] and b["start"] - a["end"] < 0.6):
                cuts.append((a["start"], b["start"]))
                dropped.add(id(a))
            elif i + 3 < len(live):
                c, d = live[i + 2], live[i + 3]
                if ([norm(a["w"]), norm(b["w"])] == [norm(c["w"]), norm(d["w"])] and norm(a["w"])
                        and len({a["speaker"], b["speaker"], c["speaker"], d["speaker"]}) == 1
                        and c["start"] - b["end"] < 0.6):
                    cuts.append((a["start"], c["start"]))
                    dropped.update({id(a), id(b)})
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
                if hi - lo > 0:
                    cuts.append((lo, hi))
            elif gap >= cfg["um_min"]:
                seg = self.env[int(a * ENV_HZ):int(b * ENV_HZ)]
                if len(seg) and np.mean(seg > speech_db - cfg["um_voice_db"]) > 0.5:
                    mutes.append((a + 0.03, b - 0.03))
        cuts = [self._snap(a, b) for a, b in merge(cuts)]
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
        self.dropped = dropped
        self.out_starts = np.cumsum([0.0] + [b - a for a, b in keeps[:-1]]).tolist()
        self.out_dur = sum(b - a for a, b in keeps)

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

    def to_src(self, T: float) -> float:
        k = max(0, bisect_right(self.out_starts, T) - 1)
        return self.keeps[k][0] + (T - self.out_starts[k])

    def chops(self) -> list:
        """Output times where a cut joins two kept stretches."""
        return self.out_starts[1:]

    def words_out(self, words: list) -> list:
        """Kept words on the output timeline (fillers and stutters left out)."""
        out = []
        for w in words:
            if w.get("silent") or not w.get("speaker") or id(w) in self.dropped:
                continue
            a, b = self.to_out(w["start"]), self.to_out(min(w["end"], w["start"] + 0.05))
            if a is None:
                continue
            end = self.to_out(w["end"])
            out.append({**w, "start": round(a, 3), "end": round(end if end is not None else a + (w["end"] - w["start"]), 3)})
        return out

    # ---- audio
    def audio(self) -> np.ndarray:
        x = self.pcm.copy()
        ramp = int(FADE * SR)
        for a, b in self.mutes:  # mute with short ramps so it doesn't click
            i, j = int(a * SR), int(b * SR)
            if j - i <= 2 * ramp:
                continue
            x[i:i + ramp] *= np.linspace(1, 0, ramp, dtype=np.float32)
            x[i + ramp:j - ramp] = 0
            x[j - ramp:j] *= np.linspace(0, 1, ramp, dtype=np.float32)
        parts = []
        for a, b in self.keeps:
            seg = x[int(a * SR):int(b * SR)].copy()
            if len(seg) > 2 * ramp:
                seg[:ramp] *= np.linspace(0, 1, ramp, dtype=np.float32)
                seg[-ramp:] *= np.linspace(1, 0, ramp, dtype=np.float32)
            parts.append(seg)
        return np.concatenate(parts) if parts else x[:0]

    def summary(self) -> str:
        muted = sum(b - a for a, b in self.mutes)
        return (f"{self.dur / 60:.1f} -> {self.out_dur / 60:.1f} min ({self.dur - self.out_dur:.0f}s cut in "
                f"{len(self.keeps) - 1} places), {len(self.mutes)} ums muted ({muted:.0f}s)")


def build(edir: Path, p: dict, words: list, dur: float) -> Edit:
    pcm = decode_audio(edir / "transcripts" / f"{p['id']}.wav", SR)
    return Edit(words, pcm, dur, settings())


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
    e = build(edir, p, words, b - a)
    print(f"[{p['id']}] {e.summary()}")


if __name__ == "__main__":
    main()
