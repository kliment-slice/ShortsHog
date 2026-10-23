"""Align mic tracks to their master and label each transcript word with a speaker.

    .venv/bin/python scripts/align.py <event> [--panel ID] [--max-lag 600]

1. Offset: FFT cross-correlation of each mic against the master's audio, at
   ALIGN_SR. Convention: mic_time = master_time + offset.
2. Speaker: for each word, RMS of every mic over [start, end] + offset, each
   mic normalised by its own loud-frame level (95th percentile) so a hot gain
   doesn't win every word. Highest normalised RMS wins.

Writes transcripts/<panel>.align.json and fills `speaker` in transcripts/<panel>.json.
"""
import argparse
import json

import numpy as np

from common import decode_audio, event_dir, load_event

ALIGN_SR = 4000
FRAME = 0.05  # seconds, for the normalisation reference level
MIN_WINDOW = 0.25  # whisper sometimes emits zero-length words; measure at least this much


def xcorr_offset(master: np.ndarray, mic: np.ndarray, sr: int, max_lag: float):
    """Return (offset_seconds, confidence). Peak lag k means mic[n + k] ~ master[n]."""
    n = len(master) + len(mic) - 1
    nfft = 1 << (n - 1).bit_length()
    a = (master - master.mean()) / (master.std() + 1e-9)
    b = (mic - mic.mean()) / (mic.std() + 1e-9)
    corr = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    # irfft output: index k >= 0 is lag k, the tail holds negative lags.
    lags = np.concatenate([np.arange(0, len(mic)), np.arange(-(len(master) - 1), 0)])
    corr = np.concatenate([corr[:len(mic)], corr[nfft - (len(master) - 1):]])
    window = np.abs(lags) <= max_lag * sr
    corr, lags = np.abs(corr[window]), lags[window]
    i = int(np.argmax(corr))
    confidence = float(corr[i] / (np.median(corr) + 1e-9))
    return lags[i] / sr, confidence


def reference_level(x: np.ndarray, sr: int) -> float:
    f = int(FRAME * sr)
    frames = x[: len(x) // f * f].reshape(-1, f)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    return float(np.percentile(rms, 95)) + 1e-9


def window_rms(x: np.ndarray, sr: int, t0: float, t1: float) -> float:
    a, b = max(0, int(t0 * sr)), min(len(x), int(t1 * sr))
    if b <= a:
        return 0.0
    seg = x[a:b]
    return float(np.sqrt((seg ** 2).mean()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("--panel")
    ap.add_argument("--max-lag", type=float, default=600.0,
                    help="max |offset| in seconds to search")
    args = ap.parse_args()

    edir = event_dir(args.event)
    ev = load_event(edir)
    for panel in ev.get("panels", []):
        if args.panel and panel["id"] != args.panel:
            continue
        tpath = edir / "transcripts" / f"{panel['id']}.json"
        transcript = json.loads(tpath.read_text())
        master = decode_audio(edir / panel["master"], ALIGN_SR)

        mics = []
        for m in panel.get("mics", []):
            audio = decode_audio(edir / m["file"], ALIGN_SR)
            offset, conf = xcorr_offset(master, audio, ALIGN_SR, args.max_lag)
            mics.append({"file": m["file"], "speaker": m["speaker"], "offset": offset,
                         "confidence": conf, "audio": audio,
                         "ref": reference_level(audio, ALIGN_SR)})
            flag = "" if conf > 8 else "  <-- LOW CONFIDENCE, check manually"
            print(f"[{panel['id']}] {m['speaker']}: offset {offset:+.3f}s "
                  f"(peak/median {conf:.1f}){flag}")

        counts = {}
        for w in transcript["words"]:
            end = max(w["end"], w["start"] + MIN_WINDOW)
            scores = [window_rms(m["audio"], ALIGN_SR, w["start"] + m["offset"],
                                 end + m["offset"]) / m["ref"] for m in mics]
            w["speaker"] = mics[int(np.argmax(scores))]["speaker"] if mics else None
            counts[w["speaker"]] = counts.get(w["speaker"], 0) + 1

        tpath.write_text(json.dumps(transcript, indent=1))
        (edir / "transcripts" / f"{panel['id']}.align.json").write_text(json.dumps(
            [{k: m[k] for k in ("file", "speaker", "offset", "confidence")} for m in mics],
            indent=1))
        print(f"[{panel['id']}] words per speaker: {counts}")


if __name__ == "__main__":
    main()
