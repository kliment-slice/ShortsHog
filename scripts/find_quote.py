"""Locate a quote (e.g. from Granola) in a panel's whisper transcript.

    .venv/bin/python scripts/find_quote.py <event> <panel> "quote text" [--pad 0.2]

Fuzzy-matches the quote against the word list, snaps out to the enclosing
sentence boundaries, and prints JSON with padded in/out seconds (whisper
timeline), the dominant speaker, the match score and the matched text.
Timestamps always come from whisper, never from the quote's source.
"""
import argparse
import json
import re
from collections import Counter
from difflib import SequenceMatcher

from common import event_dir

SENTENCE_END = re.compile(r"[.?!][\"')\]]*$")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9' ]", "", s.lower())


def best_span(words: list, quote: str):
    q = norm(quote).split()
    tokens = [norm(w["w"]) for w in words]
    n, best = len(q), (0.0, 0, 0)
    for size in {max(1, n - 3), n, n + 3}:
        for i in range(0, max(1, len(tokens) - size + 1)):
            score = SequenceMatcher(None, q, tokens[i:i + size], autojunk=False).ratio()
            if score > best[0]:
                best = (score, i, min(len(tokens), i + size) - 1)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("panel")
    ap.add_argument("quote")
    ap.add_argument("--pad", type=float, default=0.2)
    ap.add_argument("--no-snap", action="store_true", help="don't extend to sentence boundaries")
    args = ap.parse_args()

    words = json.loads((event_dir(args.event) / "transcripts" / f"{args.panel}.json")
                       .read_text())["words"]
    score, i, j = best_span(words, args.quote)
    if not args.no_snap:
        while i > 0 and not SENTENCE_END.search(words[i - 1]["w"]):
            i -= 1
        while j < len(words) - 1 and not SENTENCE_END.search(words[j]["w"]):
            j += 1
    span = words[i:j + 1]
    speaker = Counter(w["speaker"] for w in span).most_common(1)[0][0]
    print(json.dumps({
        "in": round(max(0.0, span[0]["start"] - args.pad), 2),
        "out": round(span[-1]["end"] + args.pad, 2),
        "duration": round(span[-1]["end"] - span[0]["start"] + 2 * args.pad, 2),
        "speaker": speaker,
        "score": round(score, 3),
        "text": " ".join(w["w"] for w in span),
    }, indent=1))


if __name__ == "__main__":
    main()
