"""Build each panel's loudness-normalised mic mix on the shared timeline.

    .venv/bin/python scripts/mix.py <event> [--panel ID]

Per panel: every mic is cut to the panel range (zero-padded where it isn't
rolling), gain-matched to MIC_LUFS by its measured integrated loudness (gated,
so it reflects speech rather than silence), summed, then normalised to
TARGET_LUFS. Output: transcripts/<panel>.wav, 48 kHz mono, t=0 at panel start.
This is both what whisper transcribes and the audio bed under every short.
"""
import argparse
import re
import subprocess

from common import event_dir, load_event, media, panel_range, synced

MIC_LUFS = -23.0
TARGET_LUFS = -16.0
MAX_GAIN_DB = 20.0


def mic_input(ev: dict, sid: str, start: float, end: float) -> tuple[list, float]:
    """ffmpeg input args for a mic's slice of [start, end], and the lead-in silence it needs."""
    s = synced(ev, sid)
    file_t0 = start - s["start"]
    lead = max(0.0, -file_t0)
    args = ["-ss", f"{max(0.0, file_t0):.4f}", "-t", f"{end - start - lead:.4f}",
            "-i", str(media(ev, s["file"]))]
    return args, lead


def integrated_lufs(args: list) -> float | None:
    out = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", *args, "-vn",
                          "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", out)
    return float(m[-1]) if m and m[-1] != "-inf" else None


def mix_panel(ev: dict, p: dict, out_path) -> None:
    start, end = panel_range(ev, p)
    inputs, chains = [], []
    for i, sid in enumerate(p["mics"]):
        args, lead = mic_input(ev, sid, start, end)
        lufs = integrated_lufs(args)
        gain = 0.0 if lufs is None else max(-MAX_GAIN_DB, min(MAX_GAIN_DB, MIC_LUFS - lufs))
        print(f"[{p['id']}] {sid}: {lufs} LUFS -> gain {gain:+.1f} dB, lead-in {lead:.2f}s")
        inputs += args
        chains.append(f"[{i}:a]aresample=48000,pan=mono|c0=c0,volume={gain:.2f}dB,"
                      f"adelay={int(lead * 1000)}:all=1,apad,atrim=0:{end - start:.4f}[m{i}]")
    n = len(p["mics"])
    graph = ";".join(chains) + ";" + "".join(f"[m{i}]" for i in range(n)) + \
        f"amix=inputs={n}:normalize=0,loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11,aresample=48000[out]"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs,
                    "-filter_complex", graph, "-map", "[out]", "-ac", "1",
                    "-c:a", "pcm_s16le", str(out_path)], check=True)
    print(f"[{p['id']}] mix {end - start:.1f}s -> {out_path.name}")


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
