"""Transcribe each panel's mic mix with whisper-cpp, with word-level timestamps.

    .venv/bin/python scripts/transcribe.py <event> [--panel ID]

Per panel writes:
    transcripts/<panel>.whisper.json  raw whisper-cli -ojf output per chunk (gitignored)
    transcripts/<panel>.srt           segment-level subtitles
    transcripts/<panel>.json          {panel, audio, words[], segments[]}

Input is transcripts/<panel>.wav from scripts/mix.py.

Gap refill: on a long file whisper sometimes jumps over speech (in oss-week it
skipped a 27 s question). After the full pass, every gap of REFILL_GAP seconds or
more between words is cut out with REFILL_PAD of context, the pieces are joined
with silence into one short file and transcribed again, and words that land inside
a gap are spliced in with `refill: true`. Silent gaps come back empty; anything
whisper invents there is caught by `align.py label`'s silence check.
`--refill-only` runs just this pass on an existing transcript, keeping its words.
Words carry `speaker: null` until `align.py label` fills it in.
Timestamps are seconds from the panel's start (timeline = panel start + t).
"""
import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np

import fix_words
from common import MODEL, decode_audio, event_dir, load_event

SPECIAL = re.compile(r"^\[_.*\]$|^<\|.*\|>$")
# DTW token times land ~0.1-0.3 s after the spoken onset (measured on synthetic speech
# with known onsets); shift them earlier by this much.
DTW_SHIFT = 0.15
MAX_WORD = 1.0  # s; caps a word's end when the next word is far away
REFILL_GAP = 2.5  # s between words that get a second look
REFILL_PAD = 1.0  # s of context either side of a gap
REFILL_SPACER = 1.5  # s of silence between joined gap pieces
REFILL_DEDUPE = 1.5  # s: a refilled word already present this close is context, not new
CHUNK_MIN, CHUNK_MAX = 18.0, 28.0  # s; first-pass chunks, cut at the quietest point in between
WHISPER_SR = 16000


def chunk_bounds(pcm: np.ndarray) -> list:
    """Cut points for <= CHUNK_MAX s chunks, each at the quietest moment of its last stretch."""
    hop = int(0.02 * WHISPER_SR)
    frames = len(pcm) // hop
    db = 10 * np.log10((pcm[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1) + 1e-10)
    k = int(0.3 / 0.02)  # 300 ms smoothing: a pause, not a single quiet frame
    smooth = np.convolve(db, np.ones(k) / k, mode="same")
    total = len(pcm) / WHISPER_SR
    bounds, cursor = [], 0.0
    while total - cursor > CHUNK_MAX:
        lo, hi = int((cursor + CHUNK_MIN) / 0.02), int((cursor + CHUNK_MAX) / 0.02)
        cut = (lo + int(np.argmin(smooth[lo:hi]))) * 0.02
        bounds.append((cursor, cut))
        cursor = cut
    bounds.append((cursor, total))
    return bounds


PROMPT = ""  # names and terms whisper should expect; set from event.yaml in main()


def run_whisper(audio: Path, out_base: Path, language: str) -> tuple[list, list, int]:
    """Transcribe in chunks cut at pauses. Returns (words, segments, chunk count).

    One long pass drifts: whisper skipped a 27 s question and piled runs of words onto
    one timestamp. Chunks close to its native 30 s window keep timing tight, and
    cutting at pauses means no word is split or doubled.
    """
    pcm = decode_audio(audio, WHISPER_SR)
    bounds = chunk_bounds(pcm)
    words, segments, raw = [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i, (a, b) in enumerate(bounds):
            f = Path(tmp) / f"c{i:04d}.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(WHISPER_SR),
                            "-ac", "1", "-i", "-", "-c:a", "pcm_s16le", str(f)],
                           input=pcm[int(a * WHISPER_SR):int(b * WHISPER_SR)].tobytes(), check=True)
            files.append(f)
        subprocess.run(["whisper-cli", "-m", str(MODEL), "-l", language, "-ojf", "-dtw", "large.v3.turbo",
                        "-nfa", "-np", *(["--prompt", PROMPT] if PROMPT else []),
                        *[x for f in files for x in ("-f", str(f))]],
                       check=True, capture_output=True)
        for f, (a, b) in zip(files, bounds):
            data = json.loads(Path(f"{f}.json").read_text())
            w, segs = words_from_data(data, a)
            words += w
            segments += segs
            raw.append({"start": a, "end": b, "transcription": data.get("transcription", [])})
    out_base.with_suffix(".whisper.json").write_text(json.dumps({"chunks": raw}))
    write_srt(out_base.with_suffix(".srt"), segments)
    return words, segments, len(bounds)


def write_srt(path: Path, segments: list) -> None:
    def ts(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    path.write_text("".join(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n\n"
                            for i, s in enumerate(segments, 1)))


def whisper(wav: Path, base: Path, language: str, srt: bool = False) -> Path:
    # -nfa: whisper.cpp skips DTW timing when flash attention is on.
    # No --vad: it remaps segment times but not token times in this build.
    subprocess.run(["whisper-cli", "-m", str(MODEL), "-f", str(wav), "-l", language,
                    "-ojf", *(["-osrt"] if srt else []), "-dtw", "large.v3.turbo", "-nfa", "-np",
                    *(["--prompt", PROMPT] if PROMPT else []),
                    "-of", str(base)], check=True, capture_output=True)
    return Path(f"{base}.json")


def refill_gaps(audio: Path, words: list, language: str) -> tuple[list, int]:
    """Re-transcribe the gaps between words; returns (all words, words added)."""
    pcm = decode_audio(audio, WHISPER_SR)
    total = len(pcm) / WHISPER_SR
    gaps = []
    prev_end = 0.0
    for w in words:
        if w["start"] - prev_end >= REFILL_GAP:
            gaps.append((prev_end, w["start"]))
        prev_end = max(prev_end, w["end"])
    if total - prev_end >= REFILL_GAP:
        gaps.append((prev_end, total))
    if not gaps:
        return words, 0
    pieces, index, cursor = [], [], 0.0
    spacer = np.zeros(int(REFILL_SPACER * WHISPER_SR), dtype=np.float32)
    for g0, g1 in gaps:
        a, b = max(0.0, g0 - REFILL_PAD), min(total, g1 + REFILL_PAD)
        chunk = pcm[int(a * WHISPER_SR):int(b * WHISPER_SR)]
        index.append((cursor, cursor + len(chunk) / WHISPER_SR, a, g0, g1))
        pieces += [chunk, spacer]
        cursor += (len(chunk) + len(spacer)) / WHISPER_SR
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "gaps.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(WHISPER_SR), "-ac", "1",
                        "-i", "-", "-c:a", "pcm_s16le", str(wav)], input=np.concatenate(pieces).tobytes(), check=True)
        found, _ = words_from_whisper(whisper(wav, Path(tmp) / "gaps", language))
    starts = np.array([w["start"] for w in words])

    def near(t):
        a, b = np.searchsorted(starts, [t - REFILL_DEDUPE, t + REFILL_DEDUPE])
        return words[a:b]

    added = []
    for w in found:
        for c0, c1, a, g0, g1 in index:
            if c0 <= w["start"] < c1:
                start, end = a + (w["start"] - c0), a + (min(w["end"], c1) - c0)
                dup = any(norm(x["w"]) == norm(w["w"]) and abs(x["start"] - start) < REFILL_DEDUPE for x in near(start))
                if g0 + 0.1 < start and end < g1 + 0.3 and not dup:  # inside the gap, not the context
                    added.append({**w, "start": round(start, 3), "end": round(end, 3), "refill": True})
                break
    return sorted(words + added, key=lambda w: w["start"]), len(added)


def norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def token_time(tok: dict) -> float:
    if tok.get("t_dtw", -1) >= 0:
        return max(0.0, tok["t_dtw"] / 100 - DTW_SHIFT)
    return tok["offsets"]["from"] / 1000


def words_from_whisper(whisper_json: Path) -> tuple[list, list]:
    return words_from_data(json.loads(whisper_json.read_text()), 0.0)


def words_from_data(data: dict, offset: float) -> tuple[list, list]:
    """Merge whisper tokens into words. A token starting with a space opens a new word.

    A word starts at its first token's DTW time and ends where the next word in the
    segment starts (capped at MAX_WORD), or at the segment end for the last word.
    """
    words, segments = [], []
    for seg in data.get("transcription", []):
        seg_end = offset + seg["offsets"]["to"] / 1000
        seg_words = []
        for tok in seg.get("tokens", []):
            text = tok["text"]
            if SPECIAL.match(text.strip()) or not text.strip():
                continue
            if text.startswith(" ") or not seg_words:
                seg_words.append({"w": text.strip(), "start": round(offset + token_time(tok), 3), "end": None,
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
    ap.add_argument("--refill-only", action="store_true",
                    help="only re-transcribe the gaps in the existing transcript, keeping its words")
    ap.add_argument("--no-refill", action="store_true")
    args = ap.parse_args()

    edir = event_dir(args.event)
    ev = load_event(edir)
    global PROMPT
    PROMPT = fix_words.vocabulary(ev)
    (edir / "transcripts").mkdir(exist_ok=True)
    for panel in ev.get("panels", []):
        if args.panel and panel["id"] != args.panel:
            continue
        out_base = edir / "transcripts" / panel["id"]
        mix = out_base.with_suffix(".wav")
        if not mix.is_file():
            raise SystemExit(f"{mix} missing; run scripts/mix.py first")
        if args.refill_only:
            out = json.loads(out_base.with_suffix(".json").read_text())
        else:
            print(f"[{panel['id']}] transcribing {mix.name}")
            words, segments, n_chunks = run_whisper(mix, out_base, args.language)
            print(f"[{panel['id']}] {n_chunks} chunks")
            out = {"panel": panel["id"], "audio": mix.name, "words": words, "segments": segments}
        if not args.no_refill:
            out["words"], n = refill_gaps(mix, out["words"], args.language)
            print(f"[{panel['id']}] gap refill: {n} words added")
        changes = fix_words.apply(out["words"], ev.get("corrections") or [], panel["id"])
        if changes:
            print(f"[{panel['id']}] {len(changes)} corrections from event.yaml applied")
        fix_words.rebuild_segments(out)
        write_srt(out_base.with_suffix(".srt"), out["segments"])
        out_base.with_suffix(".json").write_text(json.dumps(out, indent=1))
        print(f"[{panel['id']}] {len(out['words'])} words, {len(out['segments'])} segments")


if __name__ == "__main__":
    main()
