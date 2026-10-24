"""Build each panel's loudness-normalised mic mix on the shared timeline.

    .venv/bin/python scripts/mix.py <event> [--panel ID]

Per panel: every mic is cut to the panel range (zero-padded where it isn't
rolling) and gain-matched to MIC_LUFS by its measured integrated loudness (gated,
so it reflects speech rather than silence). Only the panel's `mix_mics` are used
(default: all its `mics`); leave out a dead mic, which only hears the room and adds
echo. Then, per the panel's `mix:`:
  sum (default)      plain sum of the mics. Best for whisper: when people share or
                     hold a mic away, every mic carries some of their voice.
  automix            gain-sharing automixer: at each moment every mic's share of
                     the output is its share of the current (gain-matched) power,
                     smoothed. The open mic is whoever is talking, so bleed from the
                     other mics doesn't pile up as echo. Gain corrections are capped
                     at MAX_RELATIVE_DB from the median mic so a dead mic isn't boosted.
                     Less bleed, but whisper did worse on it in oss-week's panel 2.
Finally normalised to TARGET_LUFS. Output: transcripts/<panel>.wav, 48 kHz mono,
t=0 at panel start. This is both what whisper transcribes and the audio bed under
every short.
"""
import argparse
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from common import decode_timeline, event_dir, load_event, media, panel_range, parts, synced

MIC_LUFS = -23.0
TARGET_LUFS = -16.0
MAX_GAIN_DB = 20.0
MAX_RELATIVE_DB = 6.0   # automix: no mic corrected more than this away from the median mic
SR = 48000
FRAME = 0.010           # automix envelope frame, seconds
ATTACK, RELEASE = 0.010, 0.300  # envelope smoothing time constants, seconds


def part_inputs(ev: dict, sid: str, start: float, end: float) -> list:
    """[(ffmpeg input args, lead-in seconds, seconds taken)] for each part of a mic in [start, end]."""
    out = []
    for p in parts(synced(ev, sid)):
        file_t0 = start - p["start"]
        lead = max(0.0, -file_t0)
        take = min(end - start - lead, p["duration"] - max(0.0, file_t0))
        if take > 0:
            out.append((["-ss", f"{max(0.0, file_t0):.4f}", "-t", f"{take:.4f}",
                         "-i", str(media(ev, p["file"]))], lead, take))
    return out


def integrated_lufs(args: list) -> float | None:
    out = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", *args, "-vn",
                          "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", out)
    return float(m[-1]) if m and m[-1] != "-inf" else None


def mic_lufs(pieces: list) -> float | None:
    """Duration-weighted loudness across a mic's parts (power average)."""
    measured = [(integrated_lufs(args), take) for args, _, take in pieces]
    measured = [(l, t) for l, t in measured if l is not None]
    if not measured:
        return None
    power = sum(t * 10 ** (l / 10) for l, t in measured) / sum(t for _, t in measured)
    return 10 * math.log10(power)


def mic_gains(ev: dict, p: dict, start: float, end: float) -> list:
    gains = []
    for sid in p.get("mix_mics") or p["mics"]:
        pieces = part_inputs(ev, sid, start, end)
        if not pieces:
            sys.exit(f"[{p['id']}] {sid} has no audio in {start:.1f}..{end:.1f}")
        lufs = mic_lufs(pieces)
        gain = 0.0 if lufs is None else max(-MAX_GAIN_DB, min(MAX_GAIN_DB, MIC_LUFS - lufs))
        gains.append((sid, pieces, lufs, gain))
    return gains


def finalize(src: Path, out_path: Path) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
                    "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11,aresample={SR}",
                    "-ac", "1", "-c:a", "pcm_s16le", str(out_path)], check=True)


def sum_mix(ev: dict, p: dict, start: float, end: float, gains: list, out_path: Path) -> None:
    dur = end - start
    inputs, chains, mics = [], [], []
    idx = 0
    for m, (sid, pieces, lufs, gain) in enumerate(gains):
        labels = []
        for args, lead, _ in pieces:
            inputs += args
            chains.append(f"[{idx}:a]aresample={SR},pan=mono|c0=c0,"
                          f"adelay={int(round(lead * 1000))}:all=1,apad,atrim=0:{dur:.4f}[p{idx}]")
            labels.append(f"[p{idx}]")
            idx += 1
        joined = "".join(labels) + (f"amix=inputs={len(labels)}:normalize=0," if len(labels) > 1 else "anull,")
        chains.append(f"{joined}volume={gain:.2f}dB[m{m}]")
        mics.append(f"[m{m}]")
    graph = ";".join(chains) + ";" + "".join(mics) + \
        f"amix=inputs={len(mics)}:normalize=0,loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11,aresample={SR}[out]"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs,
                    "-filter_complex", graph, "-map", "[out]", "-ac", "1",
                    "-c:a", "pcm_s16le", str(out_path)], check=True)


def smooth_power(power: np.ndarray) -> np.ndarray:
    """One-pole attack/release smoothing of a per-frame power envelope."""
    up, down = 1 - math.exp(-FRAME / ATTACK), 1 - math.exp(-FRAME / RELEASE)
    out = np.empty_like(power)
    acc = 0.0
    for i, x in enumerate(power):
        acc += (up if x > acc else down) * (x - acc)
        out[i] = acc
    return out


def automix(ev: dict, p: dict, start: float, end: float, gains: list, out_path: Path) -> dict:
    """Gain-sharing automix. Returns how much of the output came from non-dominant mics, per method."""
    dur = end - start
    db = np.array([g for *_, g in gains])
    db = np.clip(db, np.median(db) - MAX_RELATIVE_DB, np.median(db) + MAX_RELATIVE_DB)
    hop = int(FRAME * SR)
    n = int(round(dur * SR))
    frames = n // hop
    powers, signals = [], []
    for (sid, *_), g in zip(gains, db):
        x = decode_timeline(ev, sid, start, dur, SR)[:n] * np.float32(10 ** (g / 20))
        signals.append(x)
        pw = (x[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1)
        powers.append(smooth_power(pw))
    P = np.array(powers) + 1e-12
    W = P / P.sum(axis=0)                     # Dugan gain sharing: weights sum to 1
    t_frame = (np.arange(frames) + 0.5) * hop
    t = np.arange(n)
    out = np.zeros(n, dtype=np.float32)
    for x, w in zip(signals, W):
        out += x * np.interp(t, t_frame, w).astype(np.float32)
    # bleed share: power from mics other than the loudest, in plain sum vs automix
    dom = P.argmax(axis=0)
    total_sum = P.sum(axis=0)
    others_sum = total_sum - P.max(axis=0)
    others_auto = (W ** 2 * P).sum(axis=0) - (W.max(axis=0) ** 2) * P.max(axis=0)
    speech = total_sum > np.percentile(total_sum, 50)
    stats = {"sum": float(others_sum[speech].sum() / total_sum[speech].sum()),
             "automix": float(others_auto[speech].sum() / (W ** 2 * P).sum(axis=0)[speech].sum())}
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1",
                        "-i", "-", "-c:a", "pcm_f32le", str(raw)],
                       input=np.clip(out, -1, 1).tobytes(), check=True)
        finalize(raw, out_path)
    return stats


def mix_panel(ev: dict, p: dict, out_path: Path) -> None:
    start, end = panel_range(ev, p)
    mode = p.get("mix") or "sum"
    gains = mic_gains(ev, p, start, end)
    for sid, pieces, lufs, gain in gains:
        print(f"[{p['id']}] {sid}: {len(pieces)} part(s), "
              f"{'silent' if lufs is None else f'{lufs:.1f} LUFS'} -> gain {gain:+.1f} dB")
    if mode == "sum":
        sum_mix(ev, p, start, end, gains, out_path)
        note = ""
    elif mode == "automix":
        stats = automix(ev, p, start, end, gains, out_path)
        note = f", bleed from other mics {stats['sum']:.0%} of a plain sum -> {stats['automix']:.0%}"
    else:
        sys.exit(f"[{p['id']}] unknown mix mode `{mode}` (automix or sum)")
    print(f"[{p['id']}] {mode} {end - start:.1f}s ({start:.1f}..{end:.1f} on the timeline){note} -> {out_path.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("--panel")
    args = ap.parse_args()

    edir = event_dir(args.event)
    ev = load_event(edir)
    (edir / "transcripts").mkdir(exist_ok=True)
    for p in ev.get("panels", []):
        if not args.panel or p["id"] == args.panel:
            mix_panel(ev, p, edir / "transcripts" / f"{p['id']}.wav")


if __name__ == "__main__":
    main()
