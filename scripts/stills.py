"""Frame grabs for setting each speaker's crop.

    .venv/bin/python scripts/stills.py <event> [--at SECONDS]

Writes events/<event>/stills/:
    <camera>.jpg        a frame from each camera, full resolution, with a 100 px grid
                        (every 500 px drawn heavier) to read crop coordinates off
    <camera>.crops.jpg  the same frame with every speaker crop on that camera drawn in,
                        once speakers.<name>.crop is set
`--at` is a time in the camera file (default: a third of the way in).
"""
import argparse
import subprocess

from common import event_dir, load_event, media, probe

COLORS = ["red", "yellow", "cyan", "lime", "magenta", "orange"]


def grab(src, at: float, vf: str, out) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(src),
                    "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("--at", type=float)
    args = ap.parse_args()

    edir = event_dir(args.event)
    ev = load_event(edir)
    out = edir / "stills"
    out.mkdir(exist_ok=True)
    grid = ("drawgrid=w=100:h=100:t=1:c=white@0.35,"
            "drawgrid=w=500:h=500:t=3:c=white@0.8")
    for sid, s in ev["sources"].items():
        if s.get("kind") != "camera":
            continue
        src = media(ev, s["file"])
        info = probe(src)
        at = args.at if args.at is not None else info["duration"] / 3
        grab(src, at, grid, out / f"{sid}.jpg")
        print(f"{sid}: {info['width']}x{info['height']} @ {info['fps']:.3f} fps -> stills/{sid}.jpg (t={at:.1f}s)")

        boxes = [(name, sp["crop"]) for name, sp in (ev.get("speakers") or {}).items()
                 if sp.get("camera") == sid and sp.get("crop")]
        if boxes:
            vf = ",".join(f"drawbox=x={x}:y={y}:w={w}:h={h}:c={COLORS[i % len(COLORS)]}:t=6"
                          for i, (_, (x, y, w, h)) in enumerate(boxes))
            grab(src, at, vf, out / f"{sid}.crops.jpg")
            legend = ", ".join(f"{n}={COLORS[i % len(COLORS)]}" for i, (n, _) in enumerate(boxes))
            print(f"{sid}: crops drawn -> stills/{sid}.crops.jpg ({legend})")


if __name__ == "__main__":
    main()
