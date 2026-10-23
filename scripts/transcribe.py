"""Transcribe each panel's mic mix with whisper-cpp, with word-level timestamps.

    .venv/bin/python scripts/transcribe.py <event> [--panel ID]

Per panel writes:
    transcripts/<panel>.16k.wav       16 kHz mono input (gitignored)
    transcripts/<panel>.whisper.json  raw whisper-cli -ojf output
    transcripts/<panel>.srt           segment-level subtitles
    transcripts/<panel>.json          {panel, audio, words[], segments[]}

Input is transcripts/<panel>.wav from scripts/mix.py.
Words carry `speaker: null` until `align.py label` fills it in.
Timestamps are seconds from the panel's start (timeline = panel start + t).
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

from common import MODEL, event_dir, load_event

SPECIAL = re.compile(r"^\[_.*\]$|^<\|.*\|>$")
# DTW token times land ~0.1-0.3 s after the spoken onset (measured on synthetic speech
# with known onsets); shift them earlier by this much.
DTW_SHIFT = 0.15
MAX_WORD = 1.0  # s; caps a word's end when the next word is far away


def run_whisper(audio: Path, out_base: Path, language: str) -> Path:
    wav = out_base.with_suffix(".16k.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(audio),
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                   check=True)
    whisper_base = out_base.with_suffix(".whisper")
    # -nfa: whisper.cpp skips DTW timing when flash attention is on.
    # No --vad: it remaps segment times but not token times in this build.
    subprocess.run(["whisper-cli", "-m", str(MODEL), "-f", str(wav), "-l", language,
                    "-ojf", "-osrt", "-dtw", "large.v3.turbo", "-nfa", "-np",
                    "-of", str(whisper_base)], check=True)
    Path(f"{whisper_base}.srt").replace(out_base.with_suffix(".srt"))
    return Path(f"{whisper_base}.json")


def token_time(tok: dict) -> float:
    if tok.get("t_dtw", -1) >= 0:
        return max(0.0, tok["t_dtw"] / 100 - DTW_SHIFT)
    return tok["offsets"]["from"] / 1000


def words_from_whisper(whisper_json: Path) -> tuple[list, list]:
    """Merge whisper tokens into words. A token starting with a space opens a new word.

    A word starts at its first token's DTW time and ends where the next word in the
    segment starts (capped at MAX_WORD), or at the segment end for the last word.
    """
    data = json.loads(whisper_json.read_text())
    words, segments = [], []
    for seg in data.get("transcription", []):
        seg_end = seg["offsets"]["to"] / 1000
        seg_words = []
        for tok in seg.get("tokens", []):
            text = tok["text"]
            if SPECIAL.match(text.strip()) or not text.strip():
                continue
            if text.startswith(" ") or not seg_words:
                seg_words.append({"w": text.strip(), "start": round(token_time(tok), 3), "end": None,
                                  "p": round(tok.get("p", 1.0), 3), "speaker": None})
            else:
                seg_words[-1]["w"] += text
                seg_words[-1]["p"] = round(min(seg_words[-1]["p"], tok.get("p", 1.0)), 3)
        for i, w in enumerate(seg_words):
            nxt = seg_words[i + 1]["start"] if i + 1 < len(seg_words) else max(seg_end, w["start"] + 0.1)
            w["end"] = round(max(w["start"] + 0.05, min(nxt, w["start"] + MAX_WORD)), 3)
        if seg_words:
            segments.append({"start": seg_words[0]["start"], "end": seg_words[-1]["end"],
                             "text": seg["text"].strip()})
        words += seg_words
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
        out_base = edir / "transcripts" / panel["id"]
        mix = out_base.with_suffix(".wav")
        if not mix.is_file():
            raise SystemExit(f"{mix} missing; run scripts/mix.py first")
        print(f"[{panel['id']}] transcribing {mix.name}")
        words, segments = words_from_whisper(run_whisper(mix, out_base, args.language))
        out = {"panel": panel["id"], "audio": mix.name,
               "words": words, "segments": segments}
        out_base.with_suffix(".json").write_text(json.dumps(out, indent=1))
        print(f"[{panel['id']}] {len(words)} words, {len(segments)} segments")


if __name__ == "__main__":
    main()
