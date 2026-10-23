"""Transcribe each panel master with whisper-cpp, with word-level timestamps.

    .venv/bin/python scripts/transcribe.py <event> [--panel ID]

Per panel writes:
    transcripts/<panel>.16k.wav       16 kHz mono input (gitignored)
    transcripts/<panel>.whisper.json  raw whisper-cli -ojf output
    transcripts/<panel>.srt           segment-level subtitles
    transcripts/<panel>.json          {panel, master, words[], segments[]}

Words carry `speaker: null` until scripts/align.py fills it in.
Timestamps are seconds on the master's timeline.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

from common import MODEL, event_dir, load_event

SPECIAL = re.compile(r"^\[_.*\]$|^<\|.*\|>$")


def run_whisper(master: Path, out_base: Path, language: str) -> Path:
    wav = out_base.with_suffix(".16k.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(master),
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                   check=True)
    whisper_base = out_base.with_suffix(".whisper")
    subprocess.run(["whisper-cli", "-m", str(MODEL), "-f", str(wav), "-l", language,
                    "-ojf", "-osrt", "-dtw", "large.v3.turbo", "-np",
                    "-of", str(whisper_base)], check=True)
    Path(f"{whisper_base}.srt").replace(out_base.with_suffix(".srt"))
    return Path(f"{whisper_base}.json")


def words_from_whisper(whisper_json: Path) -> tuple[list, list]:
    """Merge whisper tokens into words. A token starting with a space opens a new word."""
    data = json.loads(whisper_json.read_text())
    words, segments = [], []
    for seg in data.get("transcription", []):
        segments.append({"start": seg["offsets"]["from"] / 1000,
                         "end": seg["offsets"]["to"] / 1000,
                         "text": seg["text"].strip()})
        for tok in seg.get("tokens", []):
            text = tok["text"]
            if SPECIAL.match(text.strip()) or not text.strip():
                continue
            start, end = tok["offsets"]["from"] / 1000, tok["offsets"]["to"] / 1000
            if text.startswith(" ") or not words:
                words.append({"w": text.strip(), "start": start, "end": end,
                              "p": round(tok.get("p", 1.0), 3), "speaker": None})
            else:
                words[-1]["w"] += text
                words[-1]["end"] = end
                words[-1]["p"] = round(min(words[-1]["p"], tok.get("p", 1.0)), 3)
    return words, segments


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("--panel")
    ap.add_argument("--language", default="en")
    args = ap.parse_args()

    edir = event_dir(args.event)
    ev = load_event(edir)
    (edir / "transcripts").mkdir(exist_ok=True)
    for panel in ev.get("panels", []):
        if args.panel and panel["id"] != args.panel:
            continue
        master = edir / panel["master"]
        out_base = edir / "transcripts" / panel["id"]
        print(f"[{panel['id']}] transcribing {master.name}")
        words, segments = words_from_whisper(run_whisper(master, out_base, args.language))
        out = {"panel": panel["id"], "master": panel["master"],
               "words": words, "segments": segments}
        out_base.with_suffix(".json").write_text(json.dumps(out, indent=1))
        print(f"[{panel['id']}] {len(words)} words, {len(segments)} segments")


if __name__ == "__main__":
    main()
