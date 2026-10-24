"""Apply word corrections from event.yaml to the transcripts.

    .venv/bin/python scripts/fix_words.py <event> [--panel ID] [--dry-run]

event.yaml `corrections:` is a list of rules:
    - {find: "postdoc", replace: "PostHog"}                       # everywhere
    - {find: "Klein", replace: "Cline", panel: gtm}               # one panel
    - {find: "client", replace: "Cline", panel: gtm, at: 466}     # the occurrence nearest 466 s
`find` matches a run of whole words, ignoring case and punctuation ("post hoc" and
"post-hoc" are different finds). `replace` may have a different number of words;
the run's time span is shared out evenly. Trailing punctuation of the matched run
is kept unless `replace` brings its own. A replaced word keeps what whisper heard in
`heard`. An empty `replace` drops the words (marked silent, kept for timing).

`transcribe.py` runs this after every transcription, so fixes survive re-transcribing.
`vocabulary:` in event.yaml (plus every name and company in the panels' `people`) is
also passed to whisper as a prompt, so these come out right the first time.
"""
import argparse
import json
import re
from pathlib import Path

from common import event_dir, load_event


PUNCT = re.compile(r"[.,?!;:…]+$")


def norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def apply(words: list, rules: list, panel_id: str) -> list:
    """Apply rules to one panel's words in place. Returns [(time, before, after)]."""
    changes = []
    for rule in rules:
        if rule.get("panel") and rule["panel"] != panel_id:
            continue
        find = [norm(x) for x in str(rule["find"]).split()]
        live = [i for i, w in enumerate(words) if not w.get("silent") or w.get("heard")]
        hits = []
        for k in range(len(live) - len(find) + 1):
            idx = live[k:k + len(find)]
            if [norm(words[i]["w"]) for i in idx] == find:
                hits.append(idx)
        if rule.get("at") is not None and hits:
            hits = [min(hits, key=lambda idx: abs(words[idx[0]]["start"] - float(rule["at"])))]
        for idx in reversed(hits):  # back to front so indices stay valid
            before = " ".join(words[i]["w"] for i in idx)
            first, last = words[idx[0]], words[idx[-1]]
            tail = PUNCT.search(last["w"])
            new = str(rule.get("replace") or "").split()
            if new and tail and not PUNCT.search(new[-1]):
                new[-1] += tail.group(0)
            a, b = first["start"], last["end"]
            if new and " ".join(new) == before:
                continue  # already right
            if not new:
                if all(words[i].get("silent") for i in idx):
                    continue  # already dropped on an earlier run
                for i in idx:
                    words[i].update(silent=True, speaker=None, heard=words[i].get("heard", words[i]["w"]))
                changes.append((a, before, "(dropped)"))
                continue
            step = (b - a) / len(new)
            repl = []
            for j, token in enumerate(new):
                repl.append({**first, "w": token, "start": round(a + j * step, 3),
                             "end": round(a + (j + 1) * step, 3), "heard": before})
            words[idx[0]:idx[-1] + 1] = repl
            changes.append((a, before, " ".join(new)))
    return sorted(changes)


def rebuild_segments(data: dict) -> None:
    """Segment text from the (corrected) words in each segment's time window."""
    words = [w for w in data["words"] if not w.get("silent")]
    for seg in data["segments"]:
        seg["text"] = " ".join(w["w"] for w in words if seg["start"] - 0.01 <= w["start"] < seg["end"] - 0.01)
    data["segments"] = [s for s in data["segments"] if s["text"]]


def vocabulary(ev: dict) -> str:
    """Prompt text for whisper: names, companies and event vocabulary."""
    terms = list(ev.get("vocabulary") or [])
    for p in ev.get("panels", []):
        for x in p.get("people", []):
            if isinstance(x, dict):
                terms += [x["name"], x.get("company") or ""]
    seen, out = set(), []
    for t in terms:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return ", ".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("--panel")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    rules = ev.get("corrections") or []
    for p in ev.get("panels", []):
        if args.panel and p["id"] != args.panel:
            continue
        path = edir / "transcripts" / f"{p['id']}.json"
        data = json.loads(path.read_text())
        changes = apply(data["words"], rules, p["id"])
        for t, a, b in changes:
            print(f"[{p['id']}] {int(t // 60)}:{t % 60:04.1f}  {a!r} -> {b!r}")
        print(f"[{p['id']}] {len(changes)} corrections")
        if not args.dry_run:
            rebuild_segments(data)
            path.write_text(json.dumps(data, indent=1))
            from transcribe import write_srt  # here, not at the top: transcribe imports this module
            write_srt(path.with_suffix(".srt"), data["segments"])


if __name__ == "__main__":
    main()
