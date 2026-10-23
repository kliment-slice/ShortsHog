"""End-to-end test on a synthetic event with known answers.

    .venv/bin/python tests/run_synth.py

Builds content/synth-test + events/synth-test (both gitignored), runs the whole
pipeline (sync, mix, transcribe, label, cut, export), then checks the render
against the known offsets, speakers and speech onsets with verify_synth.py.

It also leaves events/synth-test/exports/panel1-01.fcpxml for checking the
FCPXML import in Resolve: every V1 clip should fill the frame with one flat
colour (Alice red, Carol blue, Dave green), V2 magenta from 3.5s to 6.5s,
and no yellow, black bars or second colour anywhere.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
EVENT = REPO / "events" / "synth-test"
CONTENT = REPO / "content" / "synth-test"


def run(*args):
    print("$", " ".join(str(a) for a in args), flush=True)
    subprocess.run([PY, *map(str, args)], check=True, cwd=REPO)


def main():
    run(REPO / "tests" / "make_synth.py", CONTENT, EVENT)
    run(REPO / "scripts" / "align.py", "sync", EVENT)
    run(REPO / "scripts" / "mix.py", EVENT)
    run(REPO / "scripts" / "transcribe.py", EVENT)
    run(REPO / "scripts" / "align.py", "label", EVENT)

    words = [w for w in json.loads((EVENT / "transcripts" / "panel1.json").read_text())["words"] if w["speaker"]]
    shorts = [{"id": "panel1-01", "panel": "panel1", "in": round(words[0]["start"] - 0.2, 2),
               "out": round(words[-1]["end"] + 0.2, 2), "speaker": "Alice",
               "hook": "We deleted 40% of our dashboards and nobody noticed", "why": "synthetic test",
               "broll_hint": "dashboard"}]
    (EVENT / "shorts.json").write_text(json.dumps(shorts, indent=1) + "\n")

    run(REPO / "scripts" / "cut.py", EVENT)
    run(REPO / "scripts" / "export_fcpxml.py", EVENT, "--keep-status")
    subprocess.run(["xmllint", "--noout", EVENT / "exports" / "panel1-01.fcpxml"], check=True)
    result = subprocess.run([PY, "-I", REPO / "tests" / "verify_synth.py", EVENT], cwd=REPO)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
