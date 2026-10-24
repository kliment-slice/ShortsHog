"""Put every source on one shared timeline, and label transcript words by speaker.

    .venv/bin/python scripts/align.py sync  <event>   # cameras + mics -> sources.*.start in event.yaml
    .venv/bin/python scripts/align.py label <event> [--panel ID]

sync: each source is cross-correlated against `reference` (or its own `sync_to`,
for files that don't overlap the reference, e.g. per-panel mic files). Two passes:
  1. coarse: loudness envelopes at 100 Hz over the whole files. Robust across
     different mics (camera scratch audio vs lav), finds offsets of any size.
  2. fine: waveform at 8 kHz on the loudest 60 s of the source, +-0.25 s around
     the coarse lag.
Writes `start` (timeline second of the file's t=0), `duration` and `sync_peak`
(coarse peak over the next-best peak >0.5 s away; below ~1.3 is suspect) into each source.

label: for each word, the level (dB) of every panel mic at that moment, relative to
that mic's owner speaking (its loudest 1% of words, with the correction capped at
MAX_GAIN_DB so a mic that mostly hears bleed isn't scaled up). A Viterbi pass picks one mic per word with a
SWITCH_DB cost per change, which removes flicker but keeps real turns, even one-word
ones. The word gets that mic's speaker for the panel (`mic_speakers` on the panel,
else the source's `speaker`, else the mic id as a placeholder). If every mic is
SILENCE_DB below its speaking level, nobody was talking: whisper hallucinated the
word, so it gets `speaker: null` and `silent: true` (cut.py skips those).
"""
import argparse
import json
import sys

import numpy as np

from speakers import apply_ranges
from common import (decode_audio, decode_timeline, event_dir, load_event, media,
                    panel_range, parts, probe, save_event, source)

SR = 8000
ENV_HOP = 80  # samples per envelope frame at SR -> 100 Hz
FINE_WINDOW = 60.0
FINE_SEARCH = 0.25
LABEL_SR = 4000
SPEECH_PERCENTILE = 99
MAX_GAIN_DB = 6.0
SWITCH_DB = 6.0    # evidence (dB, summed over words) needed to change speaker; stops flicker
SILENCE_DB = 25.0  # every mic this far below its owner's level = nobody is talking
MIN_WINDOW = 0.25  # whisper sometimes emits zero-length words; measure at least this much
MIN_PEAK_RATIO = 1.3
PEAK_EXCLUSION = 50  # envelope frames (0.5 s) around the peak ignored for the runner-up


def envelope(x: np.ndarray) -> np.ndarray:
    frames = x[: len(x) // ENV_HOP * ENV_HOP].reshape(-1, ENV_HOP)
    return np.log1p(1000 * np.sqrt((frames ** 2).mean(axis=1)))


def xcorr(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Correlation of b against a for every lag. Lag k means b[n + k] ~ a[n]."""
    a = (a - a.mean()) / (a.std() + 1e-9)
    b = (b - b.mean()) / (b.std() + 1e-9)
    n = len(a) + len(b) - 1
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    # irfft output: index k >= 0 is lag k, the tail holds negative lags.
    lags = np.concatenate([np.arange(0, len(b)), np.arange(-(len(a) - 1), 0)])
    c = np.concatenate([c[:len(b)], c[nfft - (len(a) - 1):]])
    return lags, c


def offset(target: np.ndarray, src: np.ndarray) -> tuple[float, float]:
    """Seconds k such that src_time = target_time + k, and the coarse peak ratio."""
    lags, c = xcorr(envelope(target), envelope(src))
    i = int(np.argmax(c))
    rest = np.concatenate([c[:max(0, i - PEAK_EXCLUSION)], c[i + PEAK_EXCLUSION + 1:]])
    ratio = float(c[i] / rest.max()) if len(rest) and rest.max() > 0 else float("inf")
    coarse = int(lags[i]) * ENV_HOP  # in samples at SR

    # Fine pass on the loudest stretch of src that overlaps target.
    w = int(min(FINE_WINDOW, len(src) / SR / 2) * SR)
    pad = int(FINE_SEARCH * SR)
    env = envelope(src)
    hop_w = max(1, w // ENV_HOP)
    best, best_a = -1.0, None
    for a_env in range(0, max(1, len(env) - hop_w), hop_w // 2 or 1):
        a = a_env * ENV_HOP
        t = a - coarse
        if t - pad < 0 or t + w + pad > len(target):
            continue
        e = env[a_env:a_env + hop_w].sum()
        if e > best:
            best, best_a = e, a
    if best_a is None:  # overlap shorter than the window: keep the coarse result
        return coarse / SR, ratio
    seg_src = src[best_a:best_a + w]
    t = best_a - coarse
    seg_tgt = target[t - pad:t + w + pad]
    lags, c = xcorr(seg_tgt, seg_src)
    keep = np.abs(lags + pad) <= pad  # lag of 0 in this frame means src aligned at t
    lags, c = lags[keep], np.abs(c[keep])
    fine = int(lags[int(np.argmax(c))]) + pad
    return (coarse + fine) / SR, ratio


def sync(ev: dict) -> None:
    sources = ev["sources"]
    ref = ev.get("reference")
    if ref not in sources:
        sys.exit("event.yaml `reference` must name a source")
    targets = {}  # decoded audio of sources other files sync against, kept for reuse

    def target_audio(tid):
        if parts(sources[tid]) != [sources[tid]]:
            sys.exit(f"`{tid}` has parts; sync against a single-file source instead")
        if tid not in targets:
            targets[tid] = decode_audio(media(ev, sources[tid]["file"]), SR)
        return targets[tid]

    resolving = set()

    def resolve(sid):
        s = sources[sid]
        if sid == ref:
            s["start"] = 0.0
            s["duration"] = round(probe(media(ev, s["file"]))["duration"], 3)
            return
        if sid in resolving:
            sys.exit(f"sync_to cycle at `{sid}`")
        resolving.add(sid)
        target = s.get("sync_to") or ref
        source(ev, target)
        resolve(target)
        for i, part in enumerate(parts(s)):
            label = sid if part is s else f"{sid} part {i + 1}"
            if part.get("start") is None:
                k, ratio = offset(target_audio(target), decode_audio(media(ev, part["file"]), SR))
                part["start"] = round(sources[target]["start"] - k, 4)
                part["sync_peak"] = round(ratio, 2)
                flag = "" if ratio >= MIN_PEAK_RATIO else "  <-- LOW CONFIDENCE, check manually"
                print(f"{label}: start {part['start']:+.3f}s on the timeline "
                      f"(vs {target}, peak ratio {ratio:.2f}){flag}")
            part["duration"] = round(probe(media(ev, part["file"]))["duration"], 3)
        resolving.discard(sid)
        done.add(sid)

    done = set()
    for sid in list(sources):
        if sid not in done:
            resolve(sid)
    print(f"{ref}: reference, start 0")


def window_rms(x: np.ndarray, t0: float, t1: float) -> float:
    a, b = max(0, int(t0 * LABEL_SR)), min(len(x), int(t1 * LABEL_SR))
    return float(np.sqrt((x[a:b] ** 2).mean())) if b > a else 0.0


def speech_gains(levels: np.ndarray) -> np.ndarray:
    """Per-mic offsets (dB) that put each mic's owner speaking near 0 dB.

    A mic's 99th-percentile word level is usually its owner talking. When the owner
    barely speaks it's bleed instead, so the correction is capped at MAX_GAIN_DB from
    the median mic: similar mics at similar distance differ by a few dB, not 15.
    Picked on oss-week against known self-introductions (100% right) and label
    flicker, and on the synthetic test (a one-word speaker).
    """
    g = -np.percentile(levels, SPEECH_PERCENTILE, axis=0)
    mid = np.median(g)
    return np.clip(g, mid - MAX_GAIN_DB, mid + MAX_GAIN_DB)


def viterbi(scores: np.ndarray, switch: float) -> np.ndarray:
    """Most likely mic per word, given per-word scores (dB) and a cost per speaker switch."""
    n, k = scores.shape
    best = scores[0].copy()
    back = np.zeros((n, k), dtype=int)
    for t in range(1, n):
        stay = best
        move = best.max() - switch
        from_move = np.full(k, int(best.argmax()))
        take_move = move > stay
        back[t] = np.where(take_move, from_move, np.arange(k))
        best = np.where(take_move, move, stay) + scores[t]
    path = np.zeros(n, dtype=int)
    path[-1] = int(best.argmax())
    for t in range(n - 1, 0, -1):
        path[t - 1] = back[t, path[t]]
    return path


def mic_speaker(ev: dict, panel: dict, sid: str) -> str:
    """Who is on a mic in this panel: panel `mic_speakers`, else the source's `speaker`, else the mic id."""
    return (panel.get("mic_speakers") or {}).get(sid) or ev["sources"][sid].get("speaker") or sid


def label(ev: dict, edir, only: str | None) -> None:
    for p in ev.get("panels", []):
        if only and p["id"] != only:
            continue
        start, end = panel_range(ev, p)
        tpath = edir / "transcripts" / f"{p['id']}.json"
        transcript = json.loads(tpath.read_text())
        words = transcript["words"]
        names, audio = [], []
        for sid in p["mics"]:
            audio.append(decode_timeline(ev, sid, start, end - start, LABEL_SR))
            names.append(mic_speaker(ev, p, sid))
        rms = np.array([[window_rms(a, w["start"], max(w["end"], w["start"] + MIN_WINDOW)) for a in audio]
                        for w in words])
        levels = 20 * np.log10(rms + 1e-6)
        scores = np.clip(levels + speech_gains(levels), -40, 10)  # dB relative to each mic's owner
        silent = scores.max(axis=1) < -SILENCE_DB
        path = viterbi(scores, SWITCH_DB)
        counts = {}
        for w, m, quiet in zip(words, path, silent):
            if w.get("note"):  # fixed by hand (e.g. a speaker label whisper made up): leave it
                continue
            if quiet:
                w["speaker"], w["silent"] = None, True
            else:
                w["speaker"] = names[m]
                w.pop("silent", None)
            counts[w["speaker"]] = counts.get(w["speaker"], 0) + 1
        manual = edir / "transcripts" / f"{p['id']}.speakers.json"
        if manual.exists():  # people assigned from the transcript (speakers.py) win over mic labels
            apply_ranges(words, json.loads(manual.read_text()))
            counts = {}
            for w in words:
                counts[w["speaker"]] = counts.get(w["speaker"], 0) + 1
        tpath.write_text(json.dumps(transcript, indent=1))
        print(f"[{p['id']}] words per speaker: {counts}{' (with speakers.json)' if manual.exists() else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sync", "label"])
    ap.add_argument("event")
    ap.add_argument("--panel")
    ap.add_argument("--force", action="store_true", help="sync: recompute sources that already have a start")
    args = ap.parse_args()

    edir = event_dir(args.event)
    ev = load_event(edir)
    if args.cmd == "sync":
        if args.force:
            for s in ev["sources"].values():
                for part in parts(s):
                    part["start"] = None
        sync(ev)
        save_event(edir, ev)
    else:
        label(ev, edir, args.panel)


if __name__ == "__main__":
    main()
