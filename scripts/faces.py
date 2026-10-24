"""Find and follow each panelist's face, so the long-form edit can zoom on whoever is talking.

    .venv/bin/python scripts/faces.py detect  <event> [--panel ID] [--camera cam2]
    .venv/bin/python scripts/faces.py track   <event> [--panel ID] [--camera cam2]
    .venv/bin/python scripts/faces.py preview <event> <panel> [--camera cam2]

detect: samples the camera at SAMPLE_FPS over each panel's range and runs OpenCV's
YuNet face detector (models/face_detection_yunet_2023mar.onnx). It finds faces; it
doesn't recognise anyone. -> events/<e>/faces/<panel>.<camera>.json

track: names come from the panel's `seats: {<camera>: [left -> right]}` in event.yaml
(decided once by looking at who holds the mic up while the transcript says they
talk). Frames where exactly that many plausible faces are found anchor the order;
the rest are assigned to seats by fitting a scale + shift of x against the nearest
complete sample (a zoom or pan keeps the order and spacing), keeping the best fit. Each seat's centre and
face size are then interpolated over gaps and smoothed, so a camera operator's
slow zoom or pan is followed. -> events/<e>/faces/<panel>.<camera>.tracks.json

All coordinates are source pixels; times are panel time (seconds from panel start).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

from common import REPO, event_dir, load_event, media, panel, panel_range, probe, synced

MODEL = REPO / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 1.0
DETECT_W = 960          # detection resolution (width); boxes are scaled back to source pixels
MIN_SCORE = 0.6
SMOOTH_S = 5.0          # rolling-median window for seat positions
REFRAME_JUMP = 1.5      # median seat jump, in face heights, between full samples that counts as a reframe
STABLE_RUN = 10         # samples in a row with every seat found and nobody moving, to start tracking
MATCH_FACES = 1.5       # max move between samples, in face heights, to stay the same person


def out_dir(edir: Path) -> Path:
    d = edir / "faces"
    d.mkdir(exist_ok=True)
    return d


def frames(path: Path, t0: float, dur: float, w: int, h: int):
    """Yield (offset seconds, BGR frame) sampled at SAMPLE_FPS, scaled to w x h."""
    proc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                             "-vf", f"fps={SAMPLE_FPS},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                            stdout=subprocess.PIPE)
    size = w * h * 3
    i = 0
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        yield i / SAMPLE_FPS, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        i += 1
    proc.wait()


def detect(ev: dict, edir: Path, p: dict, cam_id: str) -> None:
    start, end = panel_range(ev, p)
    cam = synced(ev, cam_id)
    path = media(ev, cam["file"])
    info = probe(path)
    sw, sh = info["width"], info["height"]
    dw, dh = DETECT_W, round(DETECT_W * sh / sw / 2) * 2
    k = sw / dw
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (dw, dh), MIN_SCORE, 0.3, 5000)
    file_t0 = start - cam["start"]
    samples = []
    for off, img in frames(path, file_t0, end - start, dw, dh):
        _, faces = det.detect(img)
        boxes = [] if faces is None else [[round(float(f[0]) * k, 1), round(float(f[1]) * k, 1), round(float(f[2]) * k, 1),
                                           round(float(f[3]) * k, 1), round(float(f[14]), 3)] for f in faces]
        samples.append({"t": round(off, 3), "faces": boxes})
    out = out_dir(edir) / f"{p['id']}.{cam_id}.json"
    out.write_text(json.dumps({"camera": cam_id, "width": sw, "height": sh, "fps": SAMPLE_FPS,
                               "samples": samples}))
    counts = np.bincount([len(s["faces"]) for s in samples])
    print(f"[{p['id']}] {cam_id}: {len(samples)} samples, faces per sample: "
          + ", ".join(f"{n}:{c}" for n, c in enumerate(counts) if c))


def plausible(boxes: list, w: int, h: int) -> list:
    """Panelists, not audience heads or posters: faces of a sensible size, in the seated band."""
    out = []
    for x, y, bw, bh, score in boxes:
        cy = y + bh / 2
        if 0.03 * h <= bh <= 0.30 * h and 0.08 * h <= cy <= 0.80 * h:
            out.append((x + bw / 2, cy, bh, score))
    return sorted(out)


def auto_cuts(picked: list, t: np.ndarray, n: int) -> list:
    """Times where every seat jumps at once between full samples and the new layout holds: a reframe."""
    full = [i for i, f in enumerate(picked) if len(f) == n]
    cuts = []
    for a, b in zip(full, full[1:]):
        fa, fb = picked[a], picked[b]
        jump = np.median([abs(x[0] - y[0]) / max(x[2], y[2]) for x, y in zip(fa, fb)])
        if jump < REFRAME_JUMP:
            continue
        later = [i for i in full if i >= b][:STABLE_RUN]
        holds = len(later) == STABLE_RUN and all(
            np.median([abs(x[0] - y[0]) / max(x[2], y[2]) for x, y in zip(picked[i], fb)]) < REFRAME_JUMP / 2
            for i in later)
        if holds and (not cuts or t[b] - cuts[-1] > 2 * STABLE_RUN):
            cuts.append(float(t[b]))
    return cuts


def framings(p: dict, cam_id: str, t_end: float, auto: list = ()) -> list:
    """[(start, end, init)] from the panel's `framing.<camera>` list (times in panel seconds).

    Each entry: {start, init?: {Name: [cx, cy]}}. A camera reframe (operator zoomed or
    panned in one go) starts a new entry. Without `init`, tracking starts from the
    first sample in that framing where every seat's face is found.
    """
    spec = list(((p.get("framing") or {}).get(cam_id)) or [{"start": 0}])
    for c in auto:  # detected reframes not already listed
        if all(abs(c - float(f["start"])) > 5 for f in spec):
            spec.append({"start": c})
    spec.sort(key=lambda f: float(f["start"]))
    out = []
    for i, f in enumerate(spec):
        end = spec[i + 1]["start"] if i + 1 < len(spec) else t_end + 1
        out.append((float(f["start"]), float(end), dict(f.get("init") or {})))
    return out


def stable_start(picked: list, idx: list, n: int):
    """Position in idx at the middle of the first STABLE_RUN samples that all show every seat, still."""
    run = []
    for k, i in enumerate(idx):
        faces = picked[i]
        if len(faces) == n and (not run or all(abs(a[0] - b[0]) < 0.5 * a[2]
                                               for a, b in zip(faces, picked[idx[run[-1]]]))):
            run.append(k)
            if len(run) >= STABLE_RUN:
                return run[len(run) // 2]
        else:
            run = [k] if len(faces) == n else []
    return None


def track(ev: dict, edir: Path, p: dict, cam_id: str) -> None:
    names = ((p.get("seats") or {}).get(cam_id)) or []
    if not names:
        sys.exit(f"[{p['id']}] no seats.{cam_id} in event.yaml (names left to right)")
    raw = json.loads((out_dir(edir) / f"{p['id']}.{cam_id}.json").read_text())
    w, h = raw["width"], raw["height"]
    n = len(names)
    t = np.array([s_["t"] for s_ in raw["samples"]])
    picked = []
    for s_ in raw["samples"]:
        faces = plausible(s_["faces"], w, h)
        if len(faces) > n:  # extra faces are people further back: keep the n largest
            faces = sorted(sorted(faces, key=lambda f: -f[2])[:n])
        picked.append(faces)
    cx = np.full((len(t), n), np.nan)
    cy = np.full((len(t), n), np.nan)
    fh = np.full((len(t), n), np.nan)
    cuts = auto_cuts(picked, t, n)
    if cuts:
        print(f"[{p['id']}] {cam_id}: reframes detected at " + ", ".join(f"{c:.0f}s" for c in cuts))
    for f0, f1, init in framings(p, cam_id, float(t[-1]), cuts):
        idx = [i for i in range(len(t)) if f0 <= t[i] < f1]
        if not idx:
            continue
        state = [None] * n
        if init:
            for j, name in enumerate(names):
                if name in init:
                    state[j] = (float(init[name][0]), float(init[name][1]), 0.08 * h)
            start_at = 0
        else:
            start_at = stable_start(picked, idx, n)
            if start_at is None:
                print(f"[{p['id']}] framing from {f0:.0f}s: never saw all {n} faces still for "
                      f"{STABLE_RUN} samples; give `init` seat positions")
                continue
            state = [tuple(f[:3]) for f in picked[idx[start_at]]]
        # forwards from the starting sample, then backwards from it with the same starting state
        start_state = list(state)
        order = idx[start_at:] + [None] + idx[:start_at][::-1]
        for i in order:
            if i is None:
                state = list(start_state)
                continue
            taken = set()
            pairs = []
            for fi, f in enumerate(picked[i]):
                for j, st in enumerate(state):
                    if st is not None:
                        pairs.append((abs(f[0] - st[0]) + abs(f[1] - st[1]), fi, j))
            for d, fi, j in sorted(pairs):  # greedy nearest match; faces barely move in a second
                f = picked[i][fi]
                if fi in taken or not np.isnan(cx[i, j]) or d > MATCH_FACES * max(f[2], state[j][2]):
                    continue
                taken.add(fi)
                cx[i, j], cy[i, j], fh[i, j] = f[0], f[1], f[2]
                state[j] = tuple(f[:3])
    seats = {}
    win = max(1, int(SMOOTH_S * raw["fps"]))
    for j, name in enumerate(names):
        ok = ~np.isnan(cx[:, j])
        if ok.sum() < 2:
            print(f"[{p['id']}] {cam_id} {name}: face found in {ok.sum()} samples; no close-ups on {cam_id}")
            continue
        series = {}
        for key, arr in (("cx", cx[:, j]), ("cy", cy[:, j]), ("h", fh[:, j])):
            filled = np.interp(t, t[ok], arr[ok])
            med = np.array([np.median(filled[max(0, i - win // 2):i + win // 2 + 1]) for i in range(len(t))])
            series[key] = [round(float(v), 1) for v in np.convolve(np.pad(med, win // 2, mode="edge"),
                                                                   np.ones(win) / win, mode="valid")[:len(t)]]
        seats[name] = {**series, "seen": [bool(v) for v in ok], "coverage": round(float(ok.mean()), 3)}
        print(f"[{p['id']}] {cam_id} {name:24} seen in {ok.mean():.0%} of samples, "
              f"x {min(series['cx']):.0f}-{max(series['cx']):.0f}, face {np.median(series['h']):.0f}px")
    (out_dir(edir) / f"{p['id']}.{cam_id}.tracks.json").write_text(json.dumps(
        {"camera": cam_id, "width": w, "height": h, "t": [round(float(v), 3) for v in t], "seats": seats}))


def preview(ev: dict, edir: Path, p: dict, cam_id: str) -> None:
    tr = json.loads((out_dir(edir) / f"{p['id']}.{cam_id}.tracks.json").read_text())
    start, end = panel_range(ev, p)
    cam = synced(ev, cam_id)
    t = np.array(tr["t"])
    picks = np.linspace(t[0] + 5, t[-1] - 5, 6)
    tiles = []
    for pt in picks:
        i = int(np.argmin(abs(t - pt)))
        img = next(frames(media(ev, cam["file"]), start + pt - cam["start"], 1.5, tr["width"], tr["height"]))[1].copy()
        for name, s in tr["seats"].items():
            x, y, fh = s["cx"][i], s["cy"][i], s["h"][i]
            cv2.rectangle(img, (int(x - fh * 0.6), int(y - fh * 0.7)), (int(x + fh * 0.6), int(y + fh * 0.7)), (28, 92, 255), 4)
            cv2.putText(img, name, (int(x - fh), int(y - fh)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 3)
        cv2.putText(img, f"{pt / 60:.1f} min", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 4)
        tiles.append(cv2.resize(img, (640, 360)))
    sheet = np.vstack([np.hstack(tiles[i:i + 3]) for i in (0, 3)])
    out = out_dir(edir) / f"{p['id']}.{cam_id}.preview.jpg"
    cv2.imwrite(str(out), sheet)
    print(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["detect", "track", "preview"])
    ap.add_argument("event")
    ap.add_argument("panel_arg", nargs="?")
    ap.add_argument("--panel")
    ap.add_argument("--camera", default="cam2")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    only = args.panel or args.panel_arg
    for p in ev.get("panels", []):
        if only and p["id"] != only:
            continue
        {"detect": detect, "track": track, "preview": preview}[args.cmd](ev, edir, p, args.camera)


if __name__ == "__main__":
    main()
