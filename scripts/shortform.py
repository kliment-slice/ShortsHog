"""Turn anchor cards into 3:4 portrait shorts in the ShortsHog look.

    .venv/bin/python scripts/shortform.py <event> <panel> [--id SHORT_ID ...] [--plan-only]

Specs live under `shorts:` in events/<event>/longform/<panel>.story.yaml:
    {id, speaker, anchor, from, to, skip?}
`from` / `to` are transcript text covering the speaker's whole thought; `skip` phrases
inside the range are cut. Style: "## Short form" in FORMAT.md (plus the long-form look).

  - Tightened like the long form (edit.py: ums muted, pauses, stutters and the panel's
    editorial cuts removed), then only the short's range is kept.
  - Framing: the speaker fills about 2/3 of the frame, eyes on the upper third line,
    following their tracked face on cam2. Fast cuts: close-up, a quick reaction from a
    listener, a looser cutaway on the speaker; jump cuts punch the framing.
  - On screen: the anchor as the hook at the top all the way through, short-form
    captions in the lower third (another speaker's lines get their name), the grade,
    one stripe wipe to open and the tangerine progress bar.
Output: events/<event>/shortform/<id>.mp4 (+ .ass, .plan.json).
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from bisect import bisect_right
from pathlib import Path

import numpy as np
from ruamel.yaml import YAML

import edit as tighten
import longform as lf
from common import REPO, event_dir, ffmpeg_with_libass, load_event, media, output_fps, panel, panel_range, probe, synced


def style() -> dict:
    text = (REPO / "FORMAT.md").read_text()
    m = re.search(r"## Short form.*?```yaml\n(.*?)```", text, re.S)
    if not m:
        sys.exit("FORMAT.md needs a ```yaml block under '## Short form'")
    return {**lf.style(), **YAML(typ="safe").load(m.group(1))}


def find_in(words: list, phrase: str, a: float, b: float) -> list:
    """[(start, end)] of each occurrence of phrase whose words lie inside [a, b]."""
    live = [w for w in words if not w.get("silent") and w.get("speaker") and a - 0.01 <= w["start"] <= b + 0.01]
    toks = [tighten.norm(w["w"]) for w in live]
    q = [t for t in (tighten.norm(x) for x in phrase.split()) if t]
    out = []
    for i in range(len(toks) - len(q) + 1):
        if toks[i:i + len(q)] == q:
            out.append((live[i]["start"], live[i + len(q) - 1]["end"]))
    return out


# ---------------------------------------------------------------- plan

def plan(words: list, t0: float, t1: float, main: str, tracks: lf.Tracks, st: dict, chops: list, to_src) -> list:
    """Shots on the output timeline covering [t0, t1]."""
    pauses = [(a["end"] + b["start"]) / 2 for a, b in zip(words, words[1:]) if b["start"] - a["end"] >= 0.12]
    points = sorted(set([round(c, 3) for c in chops if t0 < c < t1] + [round(p, 3) for p in pauses]))

    def snap(t, lo, hi):
        near = [p for p in points if abs(p - t) <= st["snap"] and lo < p < hi]
        return min(near, key=lambda p: abs(p - t)) if near else t

    def seen(name, a, b):
        return tracks.seen_share(name, to_src(a), to_src(b))

    turns = lf.turns(words, t1, st["min_shot"])
    turns = [(s, max(a, t0), min(b, t1)) for s, a, b in turns if b > t0 and a < t1]
    shots, k = [], 0
    for spk, a, b in turns:
        own = spk if spk and tracks.has(spk) and seen(spk, a, b) >= 0.5 else (main if tracks.has(main) else None)
        t = a
        while t < b - 1e-3:
            if t + st["shot_max"] + st["cutaway_for"] + st["min_shot"] > b:
                shots.append({"kind": "close", "person": own, "speaker": spk, "t0": t, "t1": b})
                break
            c0 = snap(t + st["shot_max"], t + st["min_shot"], b - st["min_shot"] - st["cutaway_for"])
            shots.append({"kind": "close", "person": own, "speaker": spk, "t0": t, "t1": c0})
            kind = ("react", "medium")[k % 2]
            k += 1
            who = own
            if kind == "react":
                listeners = [n for n in tracks.seats if n != spk and seen(n, c0, c0 + st["react_for"]) >= 0.8]
                if listeners:
                    who = listeners[k % len(listeners)]
                else:
                    kind = "medium"
            hold = st["react_for"] if kind == "react" else st["cutaway_for"]
            c1 = snap(c0 + hold, c0 + st["min_shot"] * 0.8, b - st["min_shot"])
            shots.append({"kind": kind, "person": who, "speaker": spk, "t0": c0, "t1": c1})
            t = c1
    merged = []
    for s in shots:
        if merged and (merged[-1]["kind"], merged[-1]["person"]) == (s["kind"], s["person"]):
            merged[-1]["t1"] = s["t1"]
        elif s["t1"] - s["t0"] > 0.05:
            merged.append(dict(s))
    return merged


def rect(shot: dict, t_src: float, punched: bool, tracks: lf.Tracks, size: tuple, st: dict) -> tuple:
    """3:4 crop on cam2: speaker ~2/3 of the frame, eyes on the upper third line."""
    w, h = size
    aspect = st["size"][0] / st["size"][1]
    if shot["person"] is None:
        ch = h
        cw = ch * aspect
        return ((w - cw) / 2, 0.0, cw, ch)
    cx, cy, fh = tracks.at(shot["person"], t_src)
    mult = st["close_face_mult"] * (st["medium_mult"] if shot["kind"] == "medium" else 1.0)
    ch = min(h, max(h / st["max_zoom"], fh * mult))
    if punched:
        ch = min(h, ch * st["punch"])
    cw = ch * aspect
    eyes = cy - 0.12 * fh
    x0 = min(max(0.0, cx - cw / 2), w - cw)
    y0 = min(max(0.0, eyes - st["eye_line"] * ch), h - ch)
    return (x0, y0, cw, ch)


# ---------------------------------------------------------------- overlays

def caption_lines(words: list, st: dict) -> list:
    lines, cur = [], None
    for w in words:
        if cur is not None:
            gap = w["start"] - cur["end"]
            if (w["speaker"] != cur["speaker"] or gap > 0.5
                    or len(cur["text"]) + 1 + len(w["w"]) > st["caption_max_chars"]
                    or re.search(r"[.?!,]$", cur["text"]) and len(cur["text"]) > 8):
                lines.append(cur)
                cur = None
        if cur is None:
            cur = {"speaker": w["speaker"], "start": w["start"], "end": w["end"], "text": w["w"]}
        else:
            cur["text"] += " " + w["w"]
            cur["end"] = w["end"]
    if cur:
        lines.append(cur)
    out = []
    for i, ln in enumerate(lines):
        end = ln["end"] + 0.25
        if i + 1 < len(lines):
            end = min(end, lines[i + 1]["start"])
        out.append((ln["start"], max(end, ln["start"] + 0.35), ln["speaker"], ln["text"]))
    return out


def write_ass(path: Path, spec: dict, lines: list, people: dict, colors: dict, st: dict, t0: float, dur: float) -> None:
    w, h = st["size"]
    box = lf.ass_color(st["caption_box"], st["caption_box_alpha"])
    hbox = lf.ass_color(st["caption_box"], st["story_box_alpha"])
    white = lf.ass_color(st["caption_text"])
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{st['font_title_weight']},{st['caption_size']},{white},{white},{box},{box},0,0,0,0,100,100,0,0,3,14,0,2,60,60,{st['caption_margin_v']},1
Style: Hook,{st['font_title_weight']},{st['hook_size']},{white},{white},{hbox},{hbox},0,0,0,0,100,100,0,0,3,16,0,8,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    end = lf.ass_time(dur)
    spk = spec["speaker"]
    y = st["hook_y"]
    pop = "\\fad(200,0)"
    ev.append(f"Dialogue: 2,{lf.ass_time(0.2)},{end},Hook,,0,0,0,,{{\\an8\\pos({w // 2},{y}){pop}\\fn{st['font_name_weight']}"
              f"\\fs{st['hook_name_size']}\\c{lf.ass_color(colors[spk])}}}{lf.clean(lf.label(spk, people))}")
    y += st["hook_name_size"] + 30
    for ln in lf.wrap(f"“{lf.clean(spec['anchor'])}”", st["hook_wrap"]):
        ev.append(f"Dialogue: 2,{lf.ass_time(0.2)},{end},Hook,,0,0,0,,{{\\an8\\pos({w // 2},{y}){pop}}}{ln}")
        y += st["hook_size"] + 18
    prev = spk
    for a, b, who, text in lines:
        a, b = a - t0, b - t0
        if b <= 0 or a >= dur:
            continue
        text = lf.clean(text.replace("\\", "").replace("{", "(").replace("}", ")"))
        first_of_turn, prev = who != prev, who
        if who != spk and first_of_turn:  # someone else starts talking: say who, once
            text = f"{{\\c{lf.ass_color(colors.get(who, st['name_colors'][0]))}}}{who.split()[0]}: {{\\c{white}}}{text}"
        ev.append(f"Dialogue: 0,{lf.ass_time(max(0.0, a))},{lf.ass_time(min(b, dur))},Caption,,0,0,0,,{text}")
    path.write_text(head + "\n".join(ev) + "\n")


# ---------------------------------------------------------------- render

def render(ev: dict, edir: Path, p: dict, spec: dict, plan_only: bool) -> Path:
    st = style()
    out_w, out_h = st["size"]
    fps = output_fps(ev)
    pstart, pend = panel_range(ev, p)
    words_src = json.loads((edir / "transcripts" / f"{p['id']}.json").read_text())["words"]
    story = lf.load_story(edir / "longform" / f"{p['id']}.story.yaml")
    span = tighten.resolve_cuts(words_src, [{"from": spec["from"], "to": spec["to"]}])
    if not span:
        sys.exit(f"{spec['id']}: couldn't find the range {spec['from']!r} .. {spec['to']!r}")
    a, b = span[0][0], span[0][1]
    skips = [(x, y, "skip", "") for phrase in spec.get("skip") or [] for x, y in find_in(words_src, phrase, a, b)]
    editorial = tighten.resolve_cuts(words_src, story["cuts"]) + skips
    ed = tighten.Edit(words_src, tighten.decode_audio(edir / "transcripts" / f"{p['id']}.wav", tighten.SR),
                      pend - pstart, tighten.settings(), editorial)
    # pad, but never into the words either side (the tightened pauses are short)
    all_words = ed.words_out(words_src)
    a_out, b_out = ed.to_out_near(a), ed.to_out_near(b)
    before = [w["end"] for w in all_words if w["end"] <= a_out + 0.01]
    after = [w["start"] for w in all_words if w["start"] >= b_out - 0.01]
    t0 = max(0.0, a_out - st["pad_start"], (before[-1] + 0.03) if before else 0.0)
    t1 = min(ed.out_dur, b_out + st["pad_end"], (after[0] - 0.05) if after else ed.out_dur)
    dur = t1 - t0
    words = [w for w in all_words if a_out - 0.05 <= w["start"] < b_out]
    tracks = lf.Tracks(edir / "faces" / f"{p['id']}.cam2.tracks.json")
    people = {x["name"]: x for x in p.get("people", []) if isinstance(x, dict)}
    seats = list(((p.get("seats") or {}).get("cam2")) or [])
    order = seats + [n for n in people if n not in seats]
    colors = {n: st["name_colors"][i % len(st["name_colors"])] for i, n in enumerate(order)}
    chops = [c for c in ed.chops() if t0 < c < t1]
    shots = plan(words, t0, t1, spec["speaker"], tracks, st, chops, ed.to_src)

    out_dir = edir / "shortform"
    out_dir.mkdir(exist_ok=True)
    tag = spec["id"]
    (out_dir / f"{tag}.plan.json").write_text(json.dumps({"range_src": [a, b], "range_out": [t0, t1], "shots": shots}, indent=1))
    ass = out_dir / f"{tag}.ass"
    write_ass(ass, spec, caption_lines(words, st), people, colors, st, t0, dur)
    kinds = {}
    for s in shots:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    print(f"[{tag}] {dur:.1f}s ({b - a:.1f}s before tightening), {len(shots)} shots "
          f"({', '.join(f'{v} {k}' for k, v in kinds.items())}), {len(chops)} jump cuts, "
          f"median shot {np.median([s['t1'] - s['t0'] for s in shots]):.1f}s")
    if plan_only:
        return out_dir / f"{tag}.plan.json"

    info = probe(media(ev, synced(ev, "cam2")["file"]))
    size = (info["width"], info["height"])
    src0, src1 = ed.to_src(t0), ed.to_src(t1)
    dec = lf.Decoder(ev, "cam2", pstart + src0, src1 - src0, fps, size, st.get("grade") or "")
    with tempfile.TemporaryDirectory() as tmp:
        audio = ed.audio()[int(t0 * tighten.SR):int(t1 * tighten.SR)].copy()
        fade = int(0.06 * tighten.SR)
        audio[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
        audio[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        wav = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(tighten.SR), "-ac", "1",
                        "-i", "-", "-c:a", "pcm_f32le", str(wav)], input=audio.astype(np.float32).tobytes(), check=True)
        out = out_dir / f"{tag}.mp4"
        enc = subprocess.Popen(
            [ffmpeg_with_libass(), "-nostdin", "-v", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "-",
             "-i", str(wav), "-filter_complex", f"[0:v]subtitles=filename='{str(ass).replace(':', chr(92) + ':')}'[v]",
             "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)],
            stdin=subprocess.PIPE)
        frames = int(round(dur * fps))
        si = 0
        began = time.time()
        try:
            for n in range(frames):
                T = t0 + n / fps
                t_src = ed.to_src(T)
                src = dec.at(pstart + t_src)
                while si + 1 < len(shots) and shots[si + 1]["t0"] <= T:
                    si += 1
                shot = shots[si]
                jumps = bisect_right(chops, T) - bisect_right(chops, shot["t0"])
                frame = lf.warp(src, rect(shot, t_src, jumps % 2 == 1, tracks, size, st), out_w, out_h)
                if n / fps < st["wipe_seconds"] / 2:  # the short opens on the stripes clearing
                    lf.draw_wipe(frame, 0.5 + (n / fps) / st["wipe_seconds"], st["wipe_colors"])
                if st.get("progress_height"):
                    lf.draw_progress(frame, (n / fps) / dur, st)
                enc.stdin.write(frame.tobytes())
        finally:
            enc.stdin.close()
            enc.wait()
            dec.close()
    if enc.returncode:
        sys.exit(f"ffmpeg failed ({enc.returncode})")
    print(f"[{tag}] rendered in {time.time() - began:.0f}s -> {out.relative_to(edir)}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("panel")
    ap.add_argument("--id", action="append", help="only these shorts (repeatable)")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    edir = event_dir(args.event)
    ev = load_event(edir)
    p = panel(ev, args.panel)
    story = YAML(typ="safe").load((edir / "longform" / f"{p['id']}.story.yaml").read_text()) or {}
    specs = [s for s in story.get("shorts") or [] if not args.id or s["id"] in args.id]
    if not specs:
        sys.exit("no shorts in the story file (or none match --id)")
    for spec in specs:
        render(ev, edir, p, spec, args.plan_only)


if __name__ == "__main__":
    main()
