"""Assign transcript sentences to people, for mics that were shared.

    .venv/bin/python scripts/speakers.py dump  <event> <panel>          # numbered sentences with mic
    .venv/bin/python scripts/speakers.py apply <event> <panel> <file>   # write the assignment
    .venv/bin/python scripts/speakers.py set   <event> <panel> <start> <end> <name>   # one time range
    .venv/bin/python scripts/speakers.py dump  <event> <panel> --todo   # only sentences without a person

Mic levels (align.py label) say which mic was used, not who held it. When people
share a mic, read the dump and assign each run of sentences to a person by what
they say (the host asks the questions, each panelist talks about their company).

<file> is JSON: [[first_id, last_id, "Name"], ...] covering the sentences. It's
stored as time ranges in transcripts/<panel>.speakers.json, so it survives a
re-transcription that shifts sentence ids only as far as the times still hold.
`align.py label` applies speakers.json after the mic labels, so relabelling keeps it.
`set` adds one range (seconds, panel time) on top; later ranges win.
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


def person_names(edir: Path, panel: str) -> list:
    """Names from the panel's `people` in event.yaml ({name, company, role} or "Name (role)")."""
    from common import load_event
    for p in load_event(edir).get("panels", []):
        if p["id"] == panel:
            return [x["name"] if isinstance(x, dict) else re.sub(r"\s*\(.*", "", x).strip()
                    for x in p.get("people", [])]
    return []


def load(edir: Path, panel: str) -> tuple[Path, dict]:
    path = edir / "transcripts" / f"{panel}.json"
    return path, json.loads(path.read_text())


JOIN = 0.6  # s: an unassigned word this close to an assigned neighbour belongs to the same person


def apply_ranges(words: list, ranges: list) -> int:
    """Set speaker on non-silent words inside each {start, end, speaker} range. Returns words changed.

    Afterwards, a word no range covers (re-transcription nudged its timing just past a range
    edge, typically a sentence's last word) takes the person of the adjacent word when
    there's no pause between them. Words after a real pause keep their mic label.
    """
    n = 0
    covered = set()
    for r in ranges:
        for i, w in enumerate(words):
            if not w.get("silent") and w["start"] >= r["start"] - 0.01 and w["end"] <= r["end"] + 0.01:
                if w.get("speaker") != r["speaker"]:
                    n += 1
                w["speaker"] = r["speaker"]
                covered.add(i)
    live = [i for i, w in enumerate(words) if not w.get("silent")]
    for _ in range(3):  # a few passes so short runs fill from both sides
        for k, i in enumerate(live):
            if i in covered:
                continue
            w = words[i]
            prev = words[live[k - 1]] if k > 0 else None
            nxt = words[live[k + 1]] if k + 1 < len(live) else None
            src = None
            if prev is not None and live[k - 1] in covered and w["start"] - prev["end"] <= JOIN:
                src = prev
            elif nxt is not None and live[k + 1] in covered and nxt["start"] - w["end"] <= JOIN:
                src = nxt
            if src is not None:
                if w.get("speaker") != src["speaker"]:
                    n += 1
                w["speaker"] = src["speaker"]
                covered.add(i)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dump", "apply", "set"])
    ap.add_argument("event")
    ap.add_argument("panel")
    ap.add_argument("rest", nargs="*", help="apply: <file>; set: <start> <end> <name>")
    ap.add_argument("--todo", action="store_true", help="dump: only sentences not yet assigned to a person")
    args = ap.parse_args()
    edir = event_dir(args.event)
    path, transcript = load(edir, args.panel)
    words = transcript["words"]
    sents = sentences(words)

    if args.cmd == "dump":
        people = {p for p in (person_names(edir, args.panel))}
        for k, (a, b) in enumerate(sents):
            if args.todo and words[a]["speaker"] in people:
                continue
            text = " ".join(w["w"] for w in words[a:b + 1])
            flag = " [refill]" if any(w.get("refill") for w in words[a:b + 1]) else ""
            print(f"{k:4d} {words[a]['start']/60:5.2f}m {words[a]['start']:7.1f}s {words[a]['speaker']}{flag}: {text}")
        return

    store = edir / "transcripts" / f"{args.panel}.speakers.json"
    if args.cmd == "set":
        if len(args.rest) != 3:
            sys.exit("set needs <start> <end> <name>")
        start, end, name = float(args.rest[0]), float(args.rest[1]), args.rest[2]
        ranges = json.loads(store.read_text()) if store.exists() else []
        ranges.append({"start": start, "end": end, "speaker": name, "from": "set"})
        changed = apply_ranges(words, ranges[-1:])
        store.write_text(json.dumps(ranges, indent=1) + "\n")
        path.write_text(json.dumps(transcript, indent=1))
        print(f"[{args.panel}] {start}-{end}s -> {name}: {changed} words relabelled")
        return

    if len(args.rest) != 1:
        sys.exit("apply needs a JSON file of [first_id, last_id, name]")
    spans = json.loads(Path(args.rest[0]).read_text())
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
    store.write_text(json.dumps(ranges, indent=1) + "\n")
    path.write_text(json.dumps(transcript, indent=1))
    counts = {}
    for w in words:
        if not w.get("silent"):
            counts[w["speaker"]] = counts.get(w["speaker"], 0) + 1
    print(f"[{args.panel}] {len(ranges)} ranges, {changed} words relabelled; words per speaker: {counts}")


if __name__ == "__main__":
    main()
