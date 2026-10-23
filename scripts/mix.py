"""Build each panel's loudness-normalised mic mix on the shared timeline.

    .venv/bin/python scripts/mix.py <event> [--panel ID]

Per panel: every mic is cut to the panel range (zero-padded where it isn't
rolling), gain-matched to MIC_LUFS by its measured integrated loudness (gated,
so it reflects speech rather than silence), summed, then normalised to
TARGET_LUFS. Output: transcripts/<panel>.wav, 48 kHz mono, t=0 at panel start.
This is both what whisper transcribes and the audio bed under every short.
"""
import argparse
import math
import re
import subprocess
import sys

from common import event_dir, load_event, media, panel_range, parts, synced

MIC_LUFS = -23.0
TARGET_LUFS = -16.0
MAX_GAIN_DB = 20.0


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


def mix_panel(ev: dict, p: dict, out_path) -> None:
    start, end = panel_range(ev, p)
    dur = end - start
    inputs, chains, mics = [], [], []
    idx = 0
    for m, sid in enumerate(p["mics"]):
        pieces = part_inputs(ev, sid, start, end)
        if not pieces:
            sys.exit(f"[{p['id']}] {sid} has no audio in {start:.1f}..{end:.1f}")
        lufs = mic_lufs(pieces)
        gain = 0.0 if lufs is None else max(-MAX_GAIN_DB, min(MAX_GAIN_DB, MIC_LUFS - lufs))
        print(f"[{p['id']}] {sid}: {len(pieces)} part(s), "
              f"{'silent' if lufs is None else f'{lufs:.1f} LUFS'} -> gain {gain:+.1f} dB")
        labels = []
        for args, lead, _ in pieces:
            inputs += args
            chains.append(f"[{idx}:a]aresample=48000,pan=mono|c0=c0,"
                          f"adelay={int(round(lead * 1000))}:all=1,apad,atrim=0:{dur:.4f}[p{idx}]")
            labels.append(f"[p{idx}]")
            idx += 1
        joined = "".join(labels) + (f"amix=inputs={len(labels)}:normalize=0," if len(labels) > 1 else "anull,")
        chains.append(f"{joined}volume={gain:.2f}dB[m{m}]")
        mics.append(f"[m{m}]")
    graph = ";".join(chains) + ";" + "".join(mics) + \
        f"amix=inputs={len(mics)}:normalize=0,loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11,aresample=48000[out]"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs,
                    "-filter_complex", graph, "-map", "[out]", "-ac", "1",
                    "-c:a", "pcm_s16le", str(out_path)], check=True)
    print(f"[{p['id']}] mix {dur:.1f}s ({start:.1f}..{end:.1f} on the timeline) -> {out_path.name}")


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
