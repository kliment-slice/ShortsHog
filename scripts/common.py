"""Shared helpers for ShortsHog scripts.

Shared timeline: every source in event.yaml has `start`, the timeline second at
which the file's own t=0 sits (filled by `align.py sync`). The reference source
has start 0. So file_time = timeline_time - source.start.

A mic recorded as several consecutive files (recorders that split every 30 min)
is one source with `parts: [{file: ...}, ...]`; each part gets its own start.

Panel times: transcripts and shorts.json use seconds from the panel's start,
so timeline_time = panel.start + t.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parent.parent
MODEL = REPO / "models" / "ggml-large-v3-turbo.bin"
FFMPEG_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")  # has libass, for captions

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096


def event_dir(arg: str) -> Path:
    """Accept either an event name or a path to the event folder."""
    p = Path(arg)
    if not p.is_dir():
        p = REPO / "events" / arg
    if not (p / "event.yaml").is_file():
        sys.exit(f"no event.yaml in {p}")
    return p


def load_event(edir: Path) -> dict:
    return _yaml.load((edir / "event.yaml").read_text()) or {}


def save_event(edir: Path, ev: dict) -> None:
    """Write event.yaml back, keeping comments and key order."""
    with open(edir / "event.yaml", "w") as f:
        _yaml.dump(ev, f)


def media(ev: dict, rel: str) -> Path:
    """Resolve a media path from event.yaml against the event's raw footage folder.

    `content` in event.yaml is relative to the repo root, e.g. content/OSS_WEEK.
    """
    if not ev.get("content"):
        sys.exit("event.yaml has no `content:` folder")
    return REPO / ev["content"] / rel


def source(ev: dict, sid: str) -> dict:
    try:
        return ev["sources"][sid]
    except KeyError:
        sys.exit(f"unknown source `{sid}` in event.yaml")


def parts(s: dict) -> list:
    """The files that make up a source: its `parts`, or the source itself."""
    return list(s["parts"]) if s.get("parts") else [s]


def synced(ev: dict, sid: str) -> dict:
    s = source(ev, sid)
    if any(p.get("start") is None for p in parts(s)):
        sys.exit(f"source `{sid}` has no `start`; run `align.py sync` first")
    return s


def coverage(s: dict) -> tuple[float, float]:
    """First and last timeline second any part of a synced source covers."""
    ps = parts(s)
    return min(p["start"] for p in ps), max(p["start"] + p["duration"] for p in ps)


def panel_range(ev: dict, panel: dict) -> tuple[float, float]:
    """Panel start/end on the shared timeline. Defaults to the span where all its mics record."""
    spans = [coverage(synced(ev, m)) for m in panel["mics"]]
    start = panel.get("start")
    end = panel.get("end")
    if start is None:
        start = max(a for a, _ in spans)
    if end is None:
        end = min(b for _, b in spans)
    if end <= start:
        sys.exit(f"panel {panel['id']}: empty range {start}..{end}")
    return float(start), float(end)


def panel(ev: dict, pid: str) -> dict:
    for p in ev.get("panels", []):
        if p["id"] == pid:
            return p
    sys.exit(f"unknown panel `{pid}`")


def probe(path: Path) -> dict:
    """Duration, and for video the frame rate and size, via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,avg_frame_rate",
         "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    info = {"duration": float(data["format"]["duration"])}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and "width" not in info:
            num, den = (int(x) for x in s["avg_frame_rate"].split("/"))
            info.update(width=s["width"], height=s["height"], fps=num / den if den else 0.0)
    return info


def decode_audio(path: Path, sr: int, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Decode (part of) any media file to mono float32 at `sr` Hz via ffmpeg."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.4f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.4f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def decode_timeline(ev: dict, sid: str, t0: float, duration: float, sr: int) -> np.ndarray:
    """A source's audio for timeline [t0, t0+duration], zero-padded where no file is rolling."""
    n = int(round(duration * sr))
    out = np.zeros(n, dtype=np.float32)
    for p in parts(synced(ev, sid)):
        file_t0 = t0 - p["start"]
        skip = max(0.0, -file_t0)  # timeline time before this file starts
        take = min(duration - skip, p["duration"] - max(0.0, file_t0))
        if take <= 0:
            continue
        audio = decode_audio(media(ev, p["file"]), sr, max(0.0, file_t0), take)
        a = int(round(skip * sr))
        out[a:a + len(audio)] = audio[: n - a]
    return out


def ffmpeg_with_libass() -> str:
    if FFMPEG_FULL.exists():
        return str(FFMPEG_FULL)
    sys.exit("captions need ffmpeg with libass: brew install ffmpeg-full")


def require(tool: str) -> None:
    if not shutil.which(tool):
        sys.exit(f"{tool} not found on PATH")
