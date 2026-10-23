"""Shared helpers for ShortsHog scripts."""
import subprocess
import sys
from pathlib import Path

import numpy as np
import yaml

REPO = Path(__file__).resolve().parent.parent
MODEL = REPO / "models" / "ggml-large-v3-turbo.bin"


def event_dir(arg: str) -> Path:
    """Accept either an event name or a path to the event folder."""
    p = Path(arg)
    if not p.is_dir():
        p = REPO / "events" / arg
    if not (p / "event.yaml").is_file():
        sys.exit(f"no event.yaml in {p}")
    return p


def load_event(edir: Path) -> dict:
    return yaml.safe_load((edir / "event.yaml").read_text()) or {}


def media(ev: dict, rel: str) -> Path:
    """Resolve a media path from event.yaml against the event's raw footage folder.

    `content` in event.yaml is relative to the repo root, e.g. content/OSS_WEEK.
    """
    if not ev.get("content"):
        sys.exit("event.yaml has no `content:` folder")
    return REPO / ev["content"] / rel


def decode_audio(path: Path, sr: int) -> np.ndarray:
    """Decode any media file to mono float32 at `sr` Hz via ffmpeg."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)
