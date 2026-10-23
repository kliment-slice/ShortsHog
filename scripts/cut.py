"""Render approved shorts with ffmpeg: camera per speaker, mic mix, b-roll, burned captions.

    .venv/bin/python scripts/cut.py <event> [--id SHORT_ID] [--dry-run]

Per short (times in shorts.json are seconds from the panel start):
  - Speaker segments from the labelled words; a segment shorter than MIN_SEGMENT
    is folded into the previous speaker so the camera doesn't flicker.
  - Each segment: that speaker's camera (event.yaml speakers.<name>.camera) at
    the synced time, cropped to speakers.<name>.crop [x, y, w, h] in source
    pixels, scaled to fill 1080x1920.
  - Audio: transcripts/<panel>.wav (the mic mix), continuous across camera cuts.
  - B-roll: first file in event.yaml `broll` folders whose name contains
    broll_hint, centre-cropped to 9:16 over [broll_at, broll_at + broll_dur]
    (defaults 3.5s / 3s), mix audio kept underneath.
  - Captions: ASS from the whisper words, styled by the yaml block under
    "## Captions" in FORMAT.md, burned in with libass.
Writes renders/<id>.mp4 (H.264 crf 20), plus <id>.ass and <id>.plan.json.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML

from common import (REPO, event_dir, ffmpeg_with_libass, load_event, media, panel,
                    panel_range, probe, synced)

W, H = 1080, 1920
MIN_SEGMENT = 1.5   # seconds; shorter speaker turns keep the previous camera
SWITCH_LEAD = 0.1   # cut to the new speaker this long before their first word
BROLL_AT, BROLL_DUR = 3.5, 3.0
VIDEO_EXT = {".mp4", ".mov", ".mxf", ".m4v"}


def speaker_segments(words: list, t_in: float, t_out: float) -> list:
    """[(speaker, t0, t1)] covering [t_in, t_out] exactly."""
    runs = []
    for w in words:
        if w["end"] <= t_in or w["start"] >= t_out or not w.get("speaker"):
            continue
        if not runs or runs[-1][0] != w["speaker"]:
            runs.append((w["speaker"], max(t_in, w["start"] - SWITCH_LEAD)))
    if not runs:
        sys.exit(f"no labelled words in {t_in}..{t_out}; run `align.py label`")
    segs = [(spk, t_in if i == 0 else t0, runs[i + 1][1] if i + 1 < len(runs) else t_out)
            for i, (spk, t0) in enumerate(runs)]
    while True:
        short = next((i for i, s in enumerate(segs) if s[2] - s[1] < MIN_SEGMENT), None)
        if short is None or len(segs) == 1:
            return segs
        spk, t0, t1 = segs.pop(short)
        if short == 0:
            segs[0] = (segs[0][0], t0, segs[0][2])
        else:
            segs[short - 1] = (segs[short - 1][0], segs[short - 1][1], t1)
        merged = []
        for s in segs:
            if merged and merged[-1][0] == s[0]:
                merged[-1] = (s[0], merged[-1][1], s[2])
            else:
                merged.append(s)
        segs = merged


def caption_style() -> dict:
    text = (REPO / "FORMAT.md").read_text()
    m = re.search(r"## Captions.*?```yaml\n(.*?)```", text, re.S)
    if not m:
        sys.exit("FORMAT.md needs a ```yaml block under '## Captions'")
    return YAML(typ="safe").load(m.group(1))


def ass_color(hex_rgb: str) -> str:
    r, g, b = hex_rgb.lstrip("#")[0:2], hex_rgb.lstrip("#")[2:4], hex_rgb.lstrip("#")[4:6]
    return f"&H00{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def caption_lines(words: list, t_in: float, t_out: float, style: dict) -> list:
    lo, hi = style["words_per_line"]
    ws = [w for w in words if w["start"] >= t_in - 0.05 and w["end"] <= t_out + 0.05
          and not w.get("silent")]
    lines, cur = [], []
    for w in ws:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            punct = re.search(r"[.,?!;:]$", cur[-1]["w"])
            if (len(cur) >= hi or gap > style["max_gap"] or w["speaker"] != cur[-1]["speaker"]
                    or (punct and len(cur) >= lo)):
                lines.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    # A lone trailing word joins the previous line if it fits and it's the same speaker.
    out = []
    for line in lines:
        if out and len(line) < lo and len(out[-1]) + len(line) <= hi \
                and line[0]["speaker"] == out[-1][-1]["speaker"] \
                and line[0]["start"] - out[-1][-1]["end"] <= style["max_gap"]:
            out[-1] = out[-1] + line
        else:
            out.append(line)
    return out


def write_ass(path: Path, words: list, t_in: float, t_out: float) -> int:
    st = caption_style()
    lines = caption_lines(words, t_in, t_out, st)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{st['font']},{st['size']},{ass_color(st['color'])},{ass_color(st['color'])},{ass_color(st['outline_color'])},&H80000000,{-1 if st['bold'] else 0},0,0,0,100,100,0,0,1,{st['outline']},{st['shadow']},2,{st['margin_h']},{st['margin_h']},{st['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for i, line in enumerate(lines):
        start = line[0]["start"] - t_in
        end = line[-1]["end"] - t_in + st["hold"]
        if i + 1 < len(lines):
            end = min(end, lines[i + 1][0]["start"] - t_in)
        end = min(max(end, start + 0.2), t_out - t_in)
        text = " ".join(w["w"] for w in line)
        if st.get("uppercase"):
            text = text.upper()
        text = text.replace("\\", "").replace("{", "(").replace("}", ")")
        events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    path.write_text(head + "\n".join(events) + "\n")
    return len(events)


def find_broll(ev: dict, hint: str | None) -> Path | None:
    if not hint:
        return None
    for folder in ev.get("broll") or []:
        root = media(ev, folder)
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for f in files:
            if f.suffix.lower() in VIDEO_EXT and hint.lower() in f.name.lower():
                return f
    return None


def filter_path(p: Path) -> str:
    """Escape a path for use inside an ffmpeg filtergraph option."""
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def cut_short(ev: dict, edir: Path, short: dict, dry_run: bool) -> dict:
    p = panel(ev, short["panel"])
    pstart, _ = panel_range(ev, p)
    words = json.loads((edir / "transcripts" / f"{p['id']}.json").read_text())["words"]
    t_in, t_out = float(short["in"]), float(short["out"])
    dur = t_out - t_in
    segs = speaker_segments(words, t_in, t_out)

    speakers = ev.get("speakers") or {}
    cams, probes = {}, {}
    for spk, _, _ in segs:
        conf = speakers.get(spk) or {}
        if not conf.get("camera") or not conf.get("crop"):
            sys.exit(f"speaker `{spk}` needs camera and crop in event.yaml")
        cams[spk] = conf
        if conf["camera"] not in probes:
            probes[conf["camera"]] = probe(media(ev, synced(ev, conf["camera"])["file"]))
    fps = float(ev.get("fps") or probes[cams[segs[0][0]]["camera"]]["fps"])

    renders = edir / "renders"
    renders.mkdir(exist_ok=True)
    inputs, chains, plan = [], [], []
    # Snap segment boundaries to output frames so the concat adds up to the short.
    bounds = [round((t0 - t_in) * fps) for _, t0, _ in segs] + [round(dur * fps)]
    for i, (spk, t0, t1) in enumerate(segs):
        cam_id = cams[spk]["camera"]
        cam = synced(ev, cam_id)
        d = (bounds[i + 1] - bounds[i]) / fps
        file_t = pstart + t_in + bounds[i] / fps - cam["start"]
        if file_t < 0 or file_t + d > cam["duration"] + 0.01:
            sys.exit(f"{short['id']}: camera {cam_id} isn't rolling at {file_t:.2f}s")
        x, y, w, h = cams[spk]["crop"]
        inputs += ["-ss", f"{file_t:.4f}", "-t", f"{d + 1 / fps:.4f}", "-i", str(media(ev, cam["file"]))]
        chains.append(f"[{i}:v]crop={w}:{h}:{x}:{y},scale={W}:{H}:force_original_aspect_ratio=increase,"
                      f"crop={W}:{H},setsar=1,fps={fps},format=yuv420p,"
                      f"trim=end_frame={bounds[i + 1] - bounds[i]},setpts=PTS-STARTPTS[v{i}]")
        plan.append({"speaker": spk, "camera": cam_id, "t0": round(bounds[i] / fps, 3),
                     "t1": round(bounds[i + 1] / fps, 3), "camera_time": round(file_t, 3)})
    n = len(segs)
    chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[base]")

    mix = edir / "transcripts" / f"{p['id']}.wav"
    inputs += ["-ss", f"{t_in:.4f}", "-t", f"{dur:.4f}", "-i", str(mix)]
    audio_idx = n
    video = "base"

    broll = find_broll(ev, short.get("broll_hint"))
    broll_info = None
    if short.get("broll_hint") and not broll:
        broll_info = {"hint": short["broll_hint"], "used": False, "reason": "no matching file"}
    elif broll:
        at = float(short.get("broll_at", BROLL_AT))
        bd = float(short.get("broll_dur", BROLL_DUR))
        if at < 3.0 or at + bd > dur - 0.5:
            broll_info = {"hint": short["broll_hint"], "used": False,
                          "reason": f"doesn't fit: at {at}s for {bd}s in a {dur:.1f}s short"}
        else:
            inputs += ["-ss", f"{float(short.get('broll_in', 0)):.4f}", "-t", f"{bd:.4f}", "-i", str(broll)]
            chains.append(f"[{n + 1}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                          f"setsar=1,fps={fps},format=yuv420p,setpts=PTS-STARTPTS+{at}/TB[br]")
            chains.append(f"[base][br]overlay=enable='between(t,{at},{at + bd})':eof_action=pass[vb]")
            video = "vb"
            broll_info = {"hint": short["broll_hint"], "used": True, "file": broll.name, "at": at, "dur": bd}

    ass = renders / f"{short['id']}.ass"
    n_captions = write_ass(ass, words, t_in, t_out)
    chains.append(f"[{video}]subtitles=filename='{filter_path(ass)}'[vout]")

    out = renders / f"{short['id']}.mp4"
    cmd = [ffmpeg_with_libass(), "-nostdin", "-v", "error", "-y", *inputs,
           "-filter_complex", ";".join(chains), "-map", "[vout]", "-map", f"{audio_idx}:a",
           "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", f"{fps}",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
           "-t", f"{dur:.4f}", str(out)]
    result = {"id": short["id"], "duration": round(dur, 2), "fps": fps, "segments": plan,
              "broll": broll_info, "captions": n_captions, "output": str(out.relative_to(edir))}
    (renders / f"{short['id']}.plan.json").write_text(json.dumps(result, indent=1))
    if not dry_run:
        subprocess.run(cmd, check=True)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("--id", action="append", help="only these short ids (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="write plans and captions, don't render")
    args = ap.parse_args()

    edir = event_dir(args.event)
    ev = load_event(edir)
    shorts = json.loads((edir / "shorts.json").read_text())
    for short in shorts:
        if args.id and short["id"] not in args.id:
            continue
        r = cut_short(ev, edir, short, args.dry_run)
        cams = " > ".join(f"{s['speaker']}@{s['camera']}" for s in r["segments"])
        b = r["broll"]
        bnote = "" if not b else (f", b-roll {b['file']}" if b["used"] else f", b-roll skipped ({b['reason']})")
        print(f"{r['id']}: {r['duration']}s, {len(r['segments'])} segments [{cams}], "
              f"{r['captions']} captions{bnote} -> {r['output']}")


if __name__ == "__main__":
    main()
