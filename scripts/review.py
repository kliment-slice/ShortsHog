"""Local review page for an event's shorts.

    .venv/bin/python scripts/review.py <event> [--port 8765]

Serves http://127.0.0.1:<port>/ (this machine only):
  - every short in shorts.json with its render, hook, speaker, duration and status
  - approve / reject, in/out trim (with the transcript around the cut and the mic mix
    to listen to the new in/out), editable hook and caption text
  - saving writes shorts.json (previous version kept as shorts.json.bak)
  - "Re-render" runs cut.py for that short only

Statuses: pending (default) / approved / rejected / edited-in-resolve.
Edited caption text is stored per short as `captions: [{start, end, text}]` in panel
time, which cut.py uses instead of the auto lines; "Reset" removes it.
"""
import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import REPO, event_dir, load_event, panel_range
from cut import RENDER_KEYS, caption_lines, caption_style

STATUSES = ("pending", "approved", "rejected", "edited-in-resolve")
CONTEXT = 15.0  # seconds of transcript shown either side of a short
PAGE = Path(__file__).with_name("review.html")


class Review:
    def __init__(self, edir: Path):
        self.edir = edir
        self.lock = threading.Lock()
        self.renders = {}  # id -> {state, log}

    @property
    def shorts_path(self):
        return self.edir / "shorts.json"

    def shorts(self) -> list:
        return json.loads(self.shorts_path.read_text())

    def save(self, shorts: list) -> None:
        shutil.copy(self.shorts_path, self.shorts_path.with_suffix(".json.bak"))
        self.shorts_path.write_text(json.dumps(shorts, indent=1) + "\n")

    def words(self, panel_id: str) -> list:
        p = self.edir / "transcripts" / f"{panel_id}.json"
        return json.loads(p.read_text())["words"] if p.exists() else []

    def auto_captions(self, words: list, short: dict) -> list:
        """Auto caption lines in panel time, the same lines cut.py would burn in."""
        st = caption_style()
        lines = caption_lines(words, float(short["in"]), float(short["out"]), st)
        out = []
        for i, line in enumerate(lines):
            end = line[-1]["end"] + st["hold"]
            if i + 1 < len(lines):
                end = min(end, lines[i + 1][0]["start"])
            out.append({"start": round(line[0]["start"], 3), "end": round(max(end, line[0]["start"] + 0.2), 3),
                        "text": " ".join(w["w"] for w in line)})
        return out

    def state(self) -> dict:
        ev = load_event(self.edir)
        words_by_panel = {}
        items = []
        for short in self.shorts():
            sid = short["id"]
            words = words_by_panel.setdefault(short["panel"], self.words(short["panel"]))
            t_in, t_out = float(short["in"]), float(short["out"])
            render = self.edir / "renders" / f"{sid}.mp4"
            plan_path = self.edir / "renders" / f"{sid}.plan.json"
            plan = json.loads(plan_path.read_text()) if plan_path.exists() else None
            current = {k: short.get(k) for k in RENDER_KEYS}
            items.append({
                **short,
                "status": short.get("status", "pending"),
                "duration": round(t_out - t_in, 2),
                "render": f"/media/{sid}.mp4?v={int(render.stat().st_mtime)}" if render.exists() else None,
                "stale": bool(render.exists() and plan and plan.get("short") != current),
                "segments": plan["segments"] if plan else [],
                "captions_edited": bool(short.get("captions")),
                "caption_lines": short.get("captions") or self.auto_captions(words, short),
                "context": [{"w": w["w"], "start": w["start"], "end": w["end"],
                             "speaker": w.get("speaker"), "silent": bool(w.get("silent"))}
                            for w in words if t_in - CONTEXT <= w["start"] <= t_out + CONTEXT],
                "render_job": self.renders.get(sid, {"state": "idle"}),
            })
        panels = {}
        for p in ev.get("panels", []):
            try:
                start, end = panel_range(ev, p)
                panels[p["id"]] = {"duration": round(end - start, 2)}
            except SystemExit:
                panels[p["id"]] = {"duration": None}
        return {"event": ev.get("event"), "statuses": STATUSES, "panels": panels, "shorts": items}

    def update(self, sid: str, patch: dict) -> dict:
        with self.lock:
            shorts = self.shorts()
            short = next((s for s in shorts if s["id"] == sid), None)
            if not short:
                raise KeyError(sid)
            if "status" in patch:
                if patch["status"] not in STATUSES:
                    raise ValueError(f"unknown status {patch['status']}")
                short["status"] = patch["status"]
            if "hook" in patch:
                short["hook"] = str(patch["hook"]).strip()
            t_in = float(patch.get("in", short["in"]))
            t_out = float(patch.get("out", short["out"]))
            if not 0 <= t_in < t_out:
                raise ValueError(f"bad range {t_in}..{t_out}")
            short["in"], short["out"] = round(t_in, 2), round(t_out, 2)
            if "captions" in patch:
                if patch["captions"]:
                    short["captions"] = [{"start": round(float(c["start"]), 3), "end": round(float(c["end"]), 3),
                                          "text": str(c["text"])} for c in patch["captions"]]
                else:
                    short.pop("captions", None)
            self.save(shorts)
            return short

    def render(self, sid: str) -> None:
        if self.renders.get(sid, {}).get("state") == "running":
            return
        self.renders[sid] = {"state": "running", "log": ""}

        def run():
            proc = subprocess.run([sys.executable, str(REPO / "scripts" / "cut.py"), str(self.edir), "--id", sid],
                                  capture_output=True, text=True)
            self.renders[sid] = {"state": "done" if proc.returncode == 0 else "error",
                                 "log": (proc.stdout + proc.stderr).strip()[-4000:]}
        threading.Thread(target=run, daemon=True).start()


def make_handler(review: Review):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if not re.match(r'"GET /(media|audio)/', args[0] if args else ""):
                sys.stderr.write("%s\n" % (fmt % args))

        def send_json(self, data, status=HTTPStatus.OK):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path: Path):
            """Serve a file with HTTP Range support, so the browser can seek in video and audio."""
            if not path.is_file():
                return self.send_error(HTTPStatus.NOT_FOUND)
            size = path.stat().st_size
            start, end = 0, size - 1
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = size - int(m.group(2))
                end = min(end, size - 1)
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def safe_name(self, name: str) -> str:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                raise ValueError("bad name")
            return name

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/":
                    body = PAGE.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/api/state":
                    self.send_json(review.state())
                elif path.startswith("/media/"):
                    self.send_file(review.edir / "renders" / self.safe_name(path[len("/media/"):]))
                elif path.startswith("/audio/"):
                    self.send_file(review.edir / "transcripts" / self.safe_name(path[len("/audio/"):]))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except ValueError as e:
                self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self):
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                if m := re.fullmatch(r"/api/short/([A-Za-z0-9._-]+)", path):
                    review.update(m.group(1), data)
                    self.send_json(review.state())
                elif m := re.fullmatch(r"/api/render/([A-Za-z0-9._-]+)", path):
                    review.render(m.group(1))
                    self.send_json({"ok": True})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError as e:
                self.send_json({"error": f"unknown short {e}"}, HTTPStatus.NOT_FOUND)
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    edir = event_dir(args.event)
    if not (edir / "shorts.json").exists():
        sys.exit(f"{edir}/shorts.json not found; run /select first")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Review(edir)))
    print(f"Reviewing {edir.name}: http://127.0.0.1:{args.port}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
