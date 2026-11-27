"""Post sheets for the shorts: what to paste into X's upload form, in the form's order, plus a thumbnail.

    .venv/bin/python scripts/post.py <event> [panel ...] [--id SHORT_ID ...] [--no-thumbs]

For each short with a `post:` in its spec (events/<event>/longform/<panel>.story.yaml):
    post: {text, title?, description?, thumb_at?}
writes events/<event>/posts/<id>.md (post text, thumbnail, title, description, category, call to action,
embedding, downloads, restrictions) and <id>.jpg (a caption-free frame, `shortform.py --thumb`), and
events/<event>/posts/index.md. Title defaults to the short's headline / anchor, description to
"Name, role at Company, on the <panel> panel at <event>." Media settings come from event.yaml `social`.
Rules and limits: FORMAT.md "## Social posts". Fails on anything over a limit, an em dash or middot,
or a video X won't take. Nothing is posted: the sheet is for pasting in by hand.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML

import shortform as sf
from common import REPO, event_dir, load_event, panel

URL = re.compile(r"https?://\S+")
BANNED = {"—": "em dash", "–": "en dash", "·": "middot"}


def style() -> dict:
    text = (REPO / "FORMAT.md").read_text()
    m = re.search(r"^## Social posts\n.*?```yaml\n(.*?)```", text, re.S | re.M)
    if not m:
        sys.exit("FORMAT.md needs a ```yaml block under '## Social posts'")
    return YAML(typ="safe").load(m.group(1))


def x_len(text: str, st: dict) -> int:
    """Characters as X counts them: every link is x_link_len."""
    return len(URL.sub("x" * st["x_link_len"], text))


def probe_video(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration,size:stream=codec_type,codec_name,width,height", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), {})
    return {"seconds": float(d["format"]["duration"]), "mb": int(d["format"]["size"]) / 1e6,
            "video": v["codec_name"], "audio": a.get("codec_name"), "w": v["width"], "h": v["height"]}


def setting(value, on="On", off="Off") -> str:
    return on if value is True else off if value is False else str(value)


def build(ev: dict, edir: Path, p: dict, spec: dict, st: dict, thumbs: bool) -> tuple:
    """(sheet markdown, problems) for one short."""
    tag, post = spec["id"], spec["post"]
    person = next((x for x in p.get("people", []) if isinstance(x, dict) and x["name"] == spec["speaker"]), {})
    role = f"{person['role']} at {person['company']}" if person.get("role") and person.get("company") else ""
    text = post["text"].strip()
    title = (post.get("title") or spec.get("headline") or spec.get("anchor") or "").strip()
    desc = (post.get("description") or
            f"{spec['speaker']}{', ' + role if role else ''}, on the {p.get('title', p['id'])} panel at {ev.get('name', ev['event'])}.").strip()
    problems = []
    for name, value, limit in (("post text", text, st["x_text_max"]), ("title", title, st["x_title_max"]),
                               ("description", desc, st["x_description_max"])):
        n = x_len(value, st)
        if not value:
            problems.append(f"{name} is empty")
        if n > limit:
            problems.append(f"{name} is {n} characters, over {limit}")
        for ch, what in BANNED.items():
            if ch in value:
                problems.append(f"{name} has an {what}")
    video = edir / "shortform" / f"{tag}.mp4"
    if not video.exists():
        problems.append(f"no render at {video.relative_to(edir)}")
        vinfo = None
    else:
        vinfo = probe_video(video)
        if vinfo["seconds"] > st["x_video_max_seconds"]:
            problems.append(f"video is {vinfo['seconds']:.0f}s, over X's {st['x_video_max_seconds']}s")
        if vinfo["mb"] > st["x_video_max_mb"]:
            problems.append(f"video is {vinfo['mb']:.0f} MB, over X's {st['x_video_max_mb']} MB")
        if vinfo["video"] != "h264" or vinfo["audio"] != "aac":
            problems.append(f"video is {vinfo['video']}/{vinfo['audio']}; X wants h264/aac")
    thumb = edir / "posts" / f"{tag}.jpg"
    if thumbs and vinfo:
        sf.render(ev, edir, p, spec, False, thumb_at=post.get("thumb_at", sf.style()["thumb_at"]))
    soc = ev.get("social") or {}
    cta = soc.get("call_to_action")
    cta_s = f"{cta['label']}: {cta['url']}" if isinstance(cta, dict) else "None"
    geo = soc.get("geo") or "none"
    geo_s = ("None" if geo == "none" else
             f"Include: {', '.join(geo['include'])}" if isinstance(geo, dict) and geo.get("include") else
             f"Exclude: {', '.join(geo['exclude'])}" if isinstance(geo, dict) and geo.get("exclude") else str(geo))
    vline = (f"`shortform/{tag}.mp4` ({vinfo['seconds']:.1f} s, {vinfo['w']}x{vinfo['h']}, {vinfo['mb']:.0f} MB, "
             f"{vinfo['video']}/{vinfo['audio']})" if vinfo else "missing")
    md = f"""# {tag}

{spec['speaker']}{' (' + role + ')' if role else ''}, {p.get('title', p['id'])}

**Video:** {vline}

## What's happening? ({x_len(text, st)}/{st['x_text_max']})
```
{text}
```

## Media settings
- **Thumbnail:** upload `posts/{tag}.jpg`{' (frame at ' + str(post.get('thumb_at', sf.style()['thumb_at'])) + ' s)' if thumb.exists() else ' (not rendered yet)'}
- **Title** ({x_len(title, st)}/{st['x_title_max']}):
```
{title}
```
- **Description** ({x_len(desc, st)}/{st['x_description_max']}):
```
{desc}
```
- **How would you describe your video?** {soc.get('category') or 'not set (event.yaml social.category)'}
- **Call to action:** {cta_s}
- **Allow playback in embedded Posts:** {setting(soc.get('embed', True))}
- **Allow video to be downloaded:** {setting(soc.get('download', False))}
- **Content restrictions:** {setting(soc.get('content_restrictions') or 'none').capitalize()}
- **Geo-restrictions:** {geo_s}
"""
    if problems:
        md += "\n## Not ready\n" + "\n".join(f"- {x}" for x in problems) + "\n"
    return md, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("panels", nargs="*", help="panel ids (default: every panel with posts)")
    ap.add_argument("--id", action="append", help="only these shorts (repeatable)")
    ap.add_argument("--no-thumbs", action="store_true", help="don't render the thumbnails")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    st = style()
    (edir / "posts").mkdir(exist_ok=True)
    rows, failed = [], 0
    for p in ev["panels"]:
        if args.panels and p["id"] not in args.panels:
            continue
        path = edir / "longform" / f"{p['id']}.story.yaml"
        if not path.exists():
            continue
        story = YAML(typ="safe").load(path.read_text()) or {}
        for spec in story.get("shorts") or []:
            if spec.get("rejected") or not spec.get("post") or (args.id and spec["id"] not in args.id):
                continue
            md, problems = build(ev, edir, panel(ev, p["id"]), spec, st, not args.no_thumbs)
            (edir / "posts" / f"{spec['id']}.md").write_text(md)
            failed += bool(problems)
            rows.append((p["id"], spec["id"], spec["speaker"], "not ready: " + "; ".join(problems) if problems else "ready"))
            print(f"[{spec['id']}] {'NOT READY: ' + '; '.join(problems) if problems else 'ready'} -> posts/{spec['id']}.md")
    if not args.id:  # the index lists every sheet written in this run
        index = ["# Posts", "", "| Panel | Short | Speaker | Status |", "|---|---|---|---|"]
        index += [f"| {a} | [{b}]({b}.md) | {c} | {d} |" for a, b, c, d in rows]
        (edir / "posts" / "index.md").write_text("\n".join(index) + "\n")
    if failed:
        sys.exit(f"{failed} post(s) not ready")


if __name__ == "__main__":
    main()
