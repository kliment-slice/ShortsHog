"""Assign transcript sentences to people, for mics that were shared.

    .venv/bin/python scripts/speakers.py dump  <event> <panel>          # numbered sentences with mic
    .venv/bin/python scripts/speakers.py apply <event> <panel> <file>   # write the assignment

Mic levels (align.py label) say which mic was used, not who held it. When people
share a mic, read the dump and assign each run of sentences to a person by what
they say (the host asks the questions, each panelist talks about their company).

<file> is JSON: [[first_id, last_id, "Name"], ...] covering the sentences. It's
stored as time ranges in transcripts/<panel>.speakers.json, so it survives a
re-transcription that shifts sentence ids only as far as the times still hold.
`align.py label` applies speakers.json after the mic labels, so relabelling keeps it.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from common import event_dir

GAP = 1.0  # seconds of silence that also ends a sentence
END = re.compile(r"[.?!][\"')\]]*$")


def sentences(words: list) -> list:
    """[(first word index, last word index)] split on punctuation, mic changes and pauses."""
    out, start = [], None
    for i, w in enumerate(words):
        if w.get("silent"):
            continue
        if start is None:
            start = i
        nxt = next((j for j in range(i + 1, len(words)) if not words[j].get("silent")), None)
        brk = (nxt is None or END.search(w["w"]) or words[nxt]["speaker"] != w["speaker"]
               or words[nxt]["start"] - w["end"] > GAP)
        if brk:
            out.append((start, i))
            start = None
    return out


def load(edir: Path, panel: str) -> tuple[Path, dict]:
    path = edir / "transcripts" / f"{panel}.json"
    return path, json.loads(path.read_text())


def apply_ranges(words: list, ranges: list) -> int:
    """Set speaker on non-silent words inside each {start, end, speaker} range. Returns words changed."""
    n = 0
    for r in ranges:
        for w in words:
            if not w.get("silent") and w["start"] >= r["start"] - 0.01 and w["end"] <= r["end"] + 0.01:
                if w.get("speaker") != r["speaker"]:
                    n += 1
                w["speaker"] = r["speaker"]
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dump", "apply"])
    ap.add_argument("event")
    ap.add_argument("panel")
    ap.add_argument("file", nargs="?")
    args = ap.parse_args()
    edir = event_dir(args.event)
    path, transcript = load(edir, args.panel)
    words = transcript["words"]
    sents = sentences(words)

    if args.cmd == "dump":
        for k, (a, b) in enumerate(sents):
            text = " ".join(w["w"] for w in words[a:b + 1])
            print(f"{k:4d} {words[a]['start']/60:5.2f}m {words[a]['speaker']}: {text}")
        return

    if not args.file:
        sys.exit("apply needs a JSON file of [first_id, last_id, name]")
    spans = json.loads(Path(args.file).read_text())
    covered = set()
    ranges = []
    for first, last, name in spans:
        if not 0 <= first <= last < len(sents):
            sys.exit(f"bad range {first}..{last} (have {len(sents)} sentences)")
        covered.update(range(first, last + 1))
        ranges.append({"start": words[sents[first][0]]["start"], "end": words[sents[last][1]]["end"],
                       "speaker": name, "from": " ".join(w["w"] for w in words[sents[first][0]:sents[first][0] + 6])})
    missing = sorted(set(range(len(sents))) - covered)
    if missing:
        print(f"warning: {len(missing)} sentences not assigned, they keep their mic label: {missing[:20]}")
    changed = apply_ranges(words, ranges)
    (edir / "transcripts" / f"{args.panel}.speakers.json").write_text(json.dumps(ranges, indent=1) + "\n")
    path.write_text(json.dumps(transcript, indent=1))
    counts = {}
    for w in words:
        if not w.get("silent"):
            counts[w["speaker"]] = counts.get(w["speaker"], 0) + 1
    print(f"[{args.panel}] {len(ranges)} ranges, {changed} words relabelled; words per speaker: {counts}")


if __name__ == "__main__":
    main()
