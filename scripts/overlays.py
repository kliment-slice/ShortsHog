"""Anchor graphics: short stock clips (or graphics) on a track above the footage, one per beat.

    .venv/bin/python scripts/overlays.py fetch <event> <panel> [--id SHORT_ID ...] [--n 4] [--refetch] [--media image]

Beats live on each short spec in events/<event>/longform/<panel>.story.yaml:
    beats:
      - kind: stat                  # stat | punchline | topic_shift
        at: "100,000 requests"      # transcript text; the graphic lands on its first word
        tags: [money, mind blown]   # 1 to 3
        query: "stack of cash"      # optional search terms (default: the tags)
        dur: 2.0                    # optional, 1 to 3 s
        media: video                # optional: video (default) or image
        clip_in: 1.5                # optional: seconds into a video clip to start from
        key: auto                   # optional: auto (default), green, black or none: cut a clip out of
                                    # its green screen or black background so it reads like a GIF
        asset: overlays/pixabay-123.mp4   # filled by `fetch` (first candidate; swap from the sheet)
        pos: [0.78, 0.44]           # centre as a fraction of the frame; filled by shortform.py, off faces

`fetch` searches Pixabay videos by default (animations first: they read like GIFs, then film;
the 720p file), or with media: image Pixabay vectors and illustrations with a transparent background
(they read as stickers) and Pexels photos. It downloads up to --n candidates per beat into
events/<event>/overlays/ with a <file>.license.json note each, and draws
overlays/<short-id>.sheet.jpg to pick from. Only Pixabay and Pexels: GIPHY and Tenor content is
not cleared for brand posts. Keys: PIXABAY_API_KEY and PEXELS_API_KEY, from the environment or
the repo's .env. Searches are cached a day (Pixabay's terms), files forever. Only the license notes
are committed; a graphic a spec names but that's missing locally is downloaded again by its id.

shortform.py uses `place` and `Sticker` below to put each graphic on the frame.
"""
import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from ruamel.yaml import YAML

from common import REPO, event_dir, load_event, panel

KINDS = ("stat", "punchline", "topic_shift")
UA = {"User-Agent": "ShortsHog/1.0 (+https://posthog.com)"}
# story files indent lists under their key; round-trip them that way so edits stay small diffs
story_yaml = YAML()
story_yaml.preserve_quotes = True
story_yaml.width = 4096
story_yaml.indent(mapping=2, sequence=4, offset=2)

LICENSES = {
    "pixabay": ("Pixabay Content License", "https://pixabay.com/service/license-summary/"),
    "pexels": ("Pexels License", "https://www.pexels.com/license/"),
}


# ---------------------------------------------------------------- retrieval

def key(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip().strip("'\"")
    return None


def get_json(url: str, headers: dict, cache: Path) -> dict:
    if cache.exists() and time.time() - cache.stat().st_mtime < 24 * 3600:
        return json.loads(cache.read_text())
    with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **headers}), timeout=30) as r:
        data = json.loads(r.read())
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def search_pixabay(q: str, n: int, odir: Path, **filters) -> list:
    """filters: image_type (vector, illustration, photo), colors ("transparent"), or id to look one up."""
    k = key("PIXABAY_API_KEY")
    if not k or n <= 0:
        return []
    params = {"key": k, "q": q[:100], "safesearch": "true", "per_page": max(3, n), **filters}
    tag = hashlib.sha1(f"pixabay|{sorted(filters.items())}|{q}".encode()).hexdigest()[:12]
    hits = get_json("https://pixabay.com/api/?" + urllib.parse.urlencode(params), {}, odir / ".search" / f"{tag}.json")["hits"]
    return [{"source": "pixabay", "id": h["id"], "url": h["webformatURL"], "page": h["pageURL"],
             "author": h.get("user"), "kind": h.get("type"), "title": h.get("tags")} for h in hits[:n]]


def search_pexels(q: str, n: int, odir: Path, photo_id: int | None = None) -> list:
    k = key("PEXELS_API_KEY")
    if not k or n <= 0:
        return []
    if photo_id:
        url = f"https://api.pexels.com/v1/photos/{photo_id}"
    else:
        url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode({"query": q, "per_page": n, "orientation": "square"})
    tag = hashlib.sha1(url.encode()).hexdigest()[:12]
    d = get_json(url, {"Authorization": k}, odir / ".search" / f"{tag}.json")
    photos = [d] if photo_id else d["photos"]
    return [{"source": "pexels", "id": p["id"], "url": p["src"]["large"], "page": p["url"],
             "author": p.get("photographer"), "kind": "photo", "title": p.get("alt")} for p in photos[:n]]


def search_pixabay_videos(q: str, n: int, odir: Path, **filters) -> list:
    """filters: video_type (film, animation), or id to look one up. The 720p ("tiny") file."""
    k = key("PIXABAY_API_KEY")
    if not k or n <= 0:
        return []
    params = {"key": k, "q": q[:100], "safesearch": "true", "per_page": max(3, n), **filters}
    tag = hashlib.sha1(f"pixabay-video|{sorted(filters.items())}|{q}".encode()).hexdigest()[:12]
    hits = get_json("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(params), {},
                    odir / ".search" / f"{tag}.json")["hits"]
    return [{"source": "pixabay", "id": h["id"], "url": h["videos"]["tiny"]["url"], "page": h["pageURL"],
             "author": h.get("user"), "kind": h.get("type"), "title": h.get("tags"), "duration": h.get("duration")}
            for h in hits[:n] if h.get("videos", {}).get("tiny", {}).get("url")]


def restore(asset: str, edir: Path) -> bool:
    """Download a graphic the spec names again by its source id (the files aren't committed)."""
    source, _, rest = Path(asset).stem.partition("-")
    if not rest.isdigit():
        return False
    odir = edir / "overlays"
    if Path(asset).suffix == ".mp4":
        found = search_pixabay_videos("", 1, odir, id=rest)
    else:
        found = search_pixabay("", 1, odir, id=rest) if source == "pixabay" else search_pexels("", 1, odir, int(rest))
    if not found:
        return False
    note = odir / f"{Path(asset).name}.license.json"
    q = json.loads(note.read_text()).get("query", "") if note.exists() else ""
    return download(found[0], odir, q) == asset


def download(c: dict, odir: Path, query: str) -> str:
    """Cache one candidate under overlays/ with its license note; returns its path relative to the event."""
    ext = Path(urllib.parse.urlparse(c["url"]).path).suffix.lower() or ".jpg"
    name = f"{c['source']}-{c['id']}{ext}"
    path = odir / name
    if not path.exists():
        with urllib.request.urlopen(urllib.request.Request(c["url"], headers=UA), timeout=60) as r:
            path.write_bytes(r.read())
    note = odir / f"{name}.license.json"
    if not note.exists():
        lic, lic_url = LICENSES[c["source"]]
        note.write_text(json.dumps({
            "file": name, "source": c["source"], "id": c["id"], "page": c["page"], "author": c["author"],
            "title": c["title"], "license": lic, "license_url": lic_url, "query": query,
            "fetched": time.strftime("%Y-%m-%d"),
        }, indent=1) + "\n")
    return f"overlays/{name}"


def video_candidates(q: str, n: int, odir: Path) -> list:
    """Animations first (they read like GIFs), then film."""
    out, seen = [], set()
    for vt in ("animation", "film"):
        for c in search_pixabay_videos(q, n, odir, video_type=vt):
            if c["id"] not in seen:
                seen.add(c["id"])
                out.append(c)
    return out[:n]


def candidates(q: str, n: int, odir: Path) -> list:
    """Cut-out graphics first (Pixabay vectors and illustrations with a transparent background),
    then any vector or illustration, then photos (Pexels, or Pixabay without a Pexels key)."""
    out = []
    for filters in ({"image_type": "vector", "colors": "transparent"},
                    {"image_type": "illustration", "colors": "transparent"},
                    {"image_type": "vector"}, {"image_type": "illustration"}):
        out += search_pixabay(q, n, odir, **filters)
    seen, uniq = set(), []
    for c in out:
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    photos = n // 4 or 1
    uniq = uniq[:n - photos]
    more = search_pexels(q, n - len(uniq), odir) or search_pixabay(q, n - len(uniq), odir, image_type="photo")
    return (uniq + [c for c in more if c["id"] not in seen])[:n]


def contact_sheet(path: Path, edir: Path, rows: list) -> None:
    """rows: [(label, [asset paths])]; a checkerboard shows what's cut out."""
    cell, pad, head = 220, 12, 34
    cols = max([len(r[1]) for r in rows] + [1])
    W = pad + cols * (cell + pad)
    H = pad + len(rows) * (head + cell + 30 + pad)
    sheet = np.full((H, W, 3), 245, np.uint8)
    y = pad
    for label, files in rows:
        cv2.putText(sheet, label[:90], (pad, y + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (30, 30, 30), 1, cv2.LINE_AA)
        y += head
        for i, f in enumerate(files):
            x = pad + i * (cell + pad)
            board = (((np.indices((cell, cell)) // 16).sum(0) % 2) * 40 + 190).astype(np.uint8)
            tile = np.dstack([board] * 3).astype(np.float32)
            st = Sticker(edir / f, cell - 8, max_aspect=1.0)  # a video shows its middle frame
            hh, ww = st.rgb.shape[:2]
            oy, ox = (cell - hh) // 2, (cell - ww) // 2
            a = st.alpha[..., None]
            tile[oy:oy + hh, ox:ox + ww] = tile[oy:oy + hh, ox:ox + ww] * (1 - a) + st.rgb * a
            sheet[y:y + cell, x:x + cell] = tile.astype(np.uint8)
            cv2.putText(sheet, f"{i + 1} {Path(f).stem}", (x, y + cell + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (30, 30, 30), 1, cv2.LINE_AA)
        y += cell + 30 + pad
    cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])


def fetch(edir: Path, p: dict, ids: list | None, n: int, refetch: bool, media: str = "video") -> None:
    if not key("PIXABAY_API_KEY") and not key("PEXELS_API_KEY"):
        sys.exit("no PIXABAY_API_KEY or PEXELS_API_KEY in the environment or .env")
    story_path = edir / "longform" / f"{p['id']}.story.yaml"
    story = story_yaml.load(story_path.read_text())
    odir = edir / "overlays"
    odir.mkdir(exist_ok=True)
    changed = False
    for spec in story.get("shorts") or []:
        if ids and spec["id"] not in ids or not spec.get("beats"):
            continue
        rows = []
        for k, beat in enumerate(spec["beats"]):
            if beat.get("kind") not in KINDS:
                print(f"[{spec['id']}] beat {k + 1}: kind should be one of {', '.join(KINDS)}")
            q = beat.get("query") or " ".join(beat.get("tags") or [])
            if not q:
                print(f"[{spec['id']}] beat {k + 1}: no tags or query, skipped")
                continue
            have = beat.get("asset")
            want = beat.get("media") or media
            if have and (Path(have).suffix == ".mp4") != (want == "video"):
                have = None  # the media changed: search again
            if have and not refetch and not (edir / have).exists() and restore(have, edir):
                print(f"[{spec['id']}] beat {k + 1}: downloaded {have} again")
            if have and not refetch and (edir / have).exists():
                files = [have]
            else:
                found = video_candidates(q, n, odir) if want == "video" else candidates(q, n, odir)
                files = [download(c, odir, q) for c in found]
                if not files:
                    print(f"[{spec['id']}] beat {k + 1} ({q!r}): nothing found, try another query")
                    continue
                if not have or refetch or have not in files:
                    beat["asset"] = files[0]
                    changed = True
            rows.append((f"{k + 1}. {beat.get('kind')} at \"{beat.get('at')}\"  q: {q}  -> {beat['asset']}", files))
        if rows:
            sheet = odir / f"{spec['id']}.sheet.jpg"
            contact_sheet(sheet, edir, rows)
            print(f"[{spec['id']}] {len(rows)} beats -> {sheet.relative_to(edir)}")
    if changed:
        save_story(story_path, story)


def save_story(path: Path, story) -> None:
    buf = io.StringIO()  # dump first: a failed dump must not truncate the file
    story_yaml.dump(story, buf)
    path.write_text(buf.getvalue())


# ---------------------------------------------------------------- placement and drawing

def card_mask(size: tuple) -> tuple:
    """(alpha 0..255, inner) for a rounded card of size (w, h); outside inner is the white edge."""
    edge = max(2, size[0] // 40)
    r = max(4, min(size) // 8)
    mask = np.zeros((size[1], size[0]), np.uint8)
    cv2.rectangle(mask, (r, 0), (size[0] - 1 - r, size[1] - 1), 255, -1)
    cv2.rectangle(mask, (0, r), (size[0] - 1, size[1] - 1 - r), 255, -1)
    for cx, cy in ((r, r), (size[0] - 1 - r, r), (r, size[1] - 1 - r), (size[0] - 1 - r, size[1] - 1 - r)):
        cv2.circle(mask, (cx, cy), r, 255, -1, cv2.LINE_AA)
    return mask, cv2.erode(mask, np.ones((2 * edge + 1, 2 * edge + 1), np.uint8))


def drop_shadow(alpha: np.ndarray) -> np.ndarray:
    """Soft shadow so a graphic holds on busy footage."""
    w = alpha.shape[1]
    sh = cv2.GaussianBlur(alpha, (0, 0), max(1.0, w / 60))
    return np.roll(np.roll(sh, max(1, w // 50), 0), max(1, w // 80), 1) * 0.35


def video_frames(path: Path, t0: float = 0.0, dur: float | None = None, fps: float | None = None) -> list:
    """BGR frames of a clip from t0, resampled to fps (native if None); loops a clip shorter than dur."""
    cap = cv2.VideoCapture(str(path))
    native = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    if dur is None:  # one frame from the middle (contact sheets)
        cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
        ok, f = cap.read()
        cap.release()
        return [f] if ok else []
    cap.set(cv2.CAP_PROP_POS_FRAMES, min(int(t0 * native), max(0, total - 1)))
    raw = []
    while len(raw) < int((dur + 0.2) * native) + 1:
        ok, f = cap.read()
        if not ok:
            break
        raw.append(f)
    cap.release()
    if not raw:
        return []
    fps = fps or native
    return [raw[int(k * native / fps) % len(raw)] for k in range(int(round(dur * fps)) + 1)]


def detect_key(bgr: np.ndarray) -> str:
    """"green" for a green screen, "black" for a plain black background, else "none" (from the border)."""
    b = np.concatenate([bgr[:4].reshape(-1, 3), bgr[-4:].reshape(-1, 3), bgr[:, :4].reshape(-1, 3), bgr[:, -4:].reshape(-1, 3)])
    hsv = cv2.cvtColor(b[None].astype(np.uint8), cv2.COLOR_BGR2HSV)[0]
    green = (hsv[:, 0] > 35) & (hsv[:, 0] < 85) & (hsv[:, 1] > 100) & (hsv[:, 2] > 70)
    if green.mean() > 0.8:
        return "green"
    if (hsv[:, 2] < 28).mean() > 0.9:
        return "black"
    return "none"


def key_frame(bgr: np.ndarray, mode: str) -> tuple:
    """(BGR float32, alpha 0..1) with the background keyed out: chroma for green, luma for black."""
    f = bgr.astype(np.float32)
    if mode == "green":
        spill = f[..., 1] - np.maximum(f[..., 0], f[..., 2])
        alpha = 1 - np.clip((spill - 25) / 60, 0, 1)
        f[..., 1] = np.minimum(f[..., 1], np.maximum(f[..., 0], f[..., 2]) + 10)  # despill the edges
    else:
        alpha = np.clip((f.max(axis=2) - 18) / 50, 0, 1)
    return f, cv2.GaussianBlur(alpha, (0, 0), 0.8)


class Sticker:
    """A graphic scaled to a width: BGR float32 and alpha 0..1. Photos and video frames get rounded
    corners and a white edge."""

    def __init__(self, path: Path, width: int, max_aspect: float = 1.3):
        img = video_frames(path)[0] if path.suffix == ".mp4" else cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None:
            sys.exit(f"can't read overlay {path}")
        if path.suffix == ".mp4" and detect_key(img) != "none":  # show it keyed, as it will play
            rgb, a = key_frame(img, detect_key(img))
            img = np.dstack([rgb, a * 255]).astype(np.uint8)
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        h, w = img.shape[:2]
        s = min(width / w, width * max_aspect / h)
        size = (max(1, round(w * s)), max(1, round(h * s)))
        img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
        cut_out = img.shape[2] == 4 and img[..., 3].min() < 250
        if cut_out:
            self.rgb = img[..., :3].astype(np.float32)
            self.alpha = img[..., 3].astype(np.float32) / 255
        else:
            rgb = img[..., :3].astype(np.float32)
            mask, inner = card_mask(size)
            rgb[inner < 128] = 255
            self.rgb, self.alpha = rgb, mask.astype(np.float32) / 255
        self.shadow = drop_shadow(self.alpha)

    def draw(self, frame: np.ndarray, cx: float, cy: float, opacity: float, t: float = 0.0) -> None:
        blend(frame, self.rgb, self.alpha, self.shadow, cx, cy, opacity)


class Clip:
    """A video clip playing from clip_in for dur seconds at fps: cut out of its green screen or black
    background when it has one (key), else on a rounded card with a white edge."""

    def __init__(self, path: Path, width: int, fps: float, dur: float, clip_in: float = 0.0, key: str = "auto"):
        frames = video_frames(path, clip_in, dur, fps)
        if not frames:
            sys.exit(f"can't read overlay {path}")
        h, w = frames[0].shape[:2]
        size = (width, max(1, round(h * width / w)))
        frames = [cv2.resize(f, size, interpolation=cv2.INTER_AREA) for f in frames]
        self.key = detect_key(frames[0]) if key == "auto" else key
        self.fps = fps
        if self.key in ("green", "black"):
            self.frames = []
            for f in frames:
                rgb, a = key_frame(f, self.key)
                self.frames.append((rgb, a, drop_shadow(a)))
        else:
            mask, inner = card_mask(size)
            alpha = mask.astype(np.float32) / 255
            shadow = drop_shadow(alpha)
            self.frames = []
            for f in frames:
                rgb = f.astype(np.float32)
                rgb[inner < 128] = 255
                self.frames.append((rgb, alpha, shadow))
        self.alpha = self.frames[0][1]  # its size, for placement

    def draw(self, frame: np.ndarray, cx: float, cy: float, opacity: float, t: float = 0.0) -> None:
        rgb, alpha, shadow = self.frames[min(len(self.frames) - 1, max(0, int(t * self.fps)))]
        blend(frame, rgb, alpha, shadow, cx, cy, opacity)


def blend(frame: np.ndarray, rgb: np.ndarray, alpha: np.ndarray, shadow: np.ndarray, cx: float, cy: float,
          opacity: float) -> None:
    h, w = alpha.shape
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    fx0, fy0 = max(0, x0), max(0, y0)
    fx1, fy1 = min(frame.shape[1], x0 + w), min(frame.shape[0], y0 + h)
    if fx1 <= fx0 or fy1 <= fy0 or opacity <= 0:
        return
    sl = (slice(fy0 - y0, fy1 - y0), slice(fx0 - x0, fx1 - x0))
    roi = frame[fy0:fy1, fx0:fx1].astype(np.float32)
    roi *= 1 - (shadow[sl] * opacity)[..., None]
    a = (alpha[sl] * opacity)[..., None]
    frame[fy0:fy1, fx0:fx1] = (roi * (1 - a) + rgb[sl] * a).astype(np.uint8)


def motion(t: float, dur: float, st: dict) -> tuple:
    """(opacity, y offset in px) at t seconds into a beat: fade and slide up in, fade out."""
    fin, fout = st["overlay_fade_in"], st["overlay_fade_out"]
    if t < 0 or t > dur:
        return 0.0, 0.0
    u = min(1.0, t / fin)
    e = 1 - (1 - u) ** 3
    out = min(1.0, (dur - t) / fout)
    return e * out, (1 - e) * st["overlay_slide"]


def overlap(box: tuple, other: tuple) -> float:
    w = min(box[2], other[2]) - max(box[0], other[0])
    h = min(box[3], other[3]) - max(box[1], other[1])
    return max(0.0, w) * max(0.0, h)


def place(faces: list, sw: int, sh: int, size: tuple, zones: list, st: dict) -> tuple:
    """Centre (fractions of the frame) for a sw x sh graphic kept off every face box in `faces`
    (boxes from all sampled frames of the beat) and out of the keep-out `zones` (hook, captions).
    Prefers the middle band of the frame, then distance from faces."""
    W, H = size
    m = st["overlay_margin"]
    best, best_score = None, None
    for cy in np.linspace(m + sh / 2, H - m - sh / 2, 40):
        for cx in np.linspace(m + sw / 2, W - m - sw / 2, 24):
            box = (cx - sw / 2, cy - sh / 2, cx + sw / 2, cy + sh / 2)
            if any(overlap(box, z) > 0 for z in zones):
                continue
            hit = sum(overlap(box, f) for f in faces)
            gap = min([max(f[0] - box[2], box[0] - f[2], f[1] - box[3], box[1] - f[3]) for f in faces] + [400])
            score = (-hit, min(gap, 160) - 0.35 * abs(cy - 0.5 * H))
            if best_score is None or score > best_score:
                best, best_score = (cx, cy), score
    if best is None:
        best = (W - m - sw / 2, H / 2)
    return round(float(best[0]) / W, 3), round(float(best[1]) / H, 3)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="search, download and license-note candidates for each beat")
    f.add_argument("event")
    f.add_argument("panel")
    f.add_argument("--id", action="append", help="only these shorts (repeatable)")
    f.add_argument("--n", type=int, default=4, help="candidates per beat")
    f.add_argument("--refetch", action="store_true", help="search again even where a beat has an asset")
    f.add_argument("--media", choices=("video", "image"), default="video", help="for beats without `media`")
    args = ap.parse_args()
    edir = event_dir(args.event)
    fetch(edir, panel(load_event(edir), args.panel), args.id, args.n, args.refetch, args.media)


if __name__ == "__main__":
    main()
