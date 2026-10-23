"""Export shorts as FCPXML timelines for hand editing in DaVinci Resolve.

    .venv/bin/python scripts/export_fcpxml.py <event> [--id SHORT_ID ...]

Per short, in events/<event>/exports/:
    <id>.fcpxml       1080x1920 timeline at the event's output fps (default 23.976), referencing
                      the original files at full resolution:
                        V1  speaker segments from the camera files at their synced times, each
                            with its crop as a transform (scale + position), camera audio off
                        V2  b-roll, if the short has one that fits
                        A1  the panel's mic mix
    <id>.srt          captions (Resolve: import into the media pool, drag onto a subtitle track)
    <id>.resolve.txt  the same crops as Resolve Inspector values (Zoom, Position X/Y), for
                      typing in by hand if Resolve drops the FCPXML transforms

Uses the same plan as cut.py, so cuts, crops and audio match the ffmpeg render.
Import in Resolve with File > Import > Timeline. Export is one-way: once a short is
edited in Resolve, that version is the final one, and the short's status in
shorts.json becomes `edited-in-resolve`.

Coordinates: sources are conformed with "fit" (a 1920x1080 frame scaled to 1080 wide).
FCPXML scale multiplies that; FCPXML position is in percent of the timeline height,
origin at the centre, y up.
"""
import argparse
import json
import shutil
import sys
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import quoteattr

from common import event_dir, load_event, media, probe
from cut import H, W, plan_short, write_srt


def frame_duration(fps: float) -> Fraction:
    for nominal in (24, 30, 60):
        if abs(fps - nominal * 1000 / 1001) < 0.01:
            return Fraction(1001, nominal * 1000)
    return Fraction(1, round(fps)) if abs(fps - round(fps)) < 0.01 else Fraction(1 / fps).limit_denominator(100000)


def rt(t: Fraction) -> str:
    """FCPXML rational time."""
    t = Fraction(t)
    return f"{t.numerator}s" if t.denominator == 1 else f"{t.numerator}/{t.denominator}s"


def timecode_start(tc: str | None, fd: Fraction) -> Fraction:
    """Embedded start timecode as a time, so clip starts line up with the file's own timecode."""
    if not tc:
        return Fraction(0)
    if ";" in tc:
        sys.exit(f"drop-frame timecode {tc} isn't supported")
    h, m, s, f = (int(x) for x in tc.split(":"))
    nominal = round(1 / fd)
    return (((h * 60 + m) * 60 + s) * nominal + f) * fd


def transform(crop: list, src_w: int, src_h: int) -> tuple[float, float, float, dict]:
    """FCPXML scale and position for a crop, plus the equivalent Resolve Inspector values."""
    x, y, w, h = crop
    fit = min(W / src_w, H / src_h)        # what "fit" conform does on its own
    fill = max(W / w, H / h)               # total scale that makes the crop fill the frame
    scale = fill / fit
    dx = (x + w / 2) - src_w / 2           # crop centre relative to source centre, px
    dy = (y + h / 2) - src_h / 2
    px, py = -dx * fill, dy * fill         # timeline px, y up
    return scale, px / H * 100, py / H * 100, {"zoom": round(scale, 4), "x": round(px, 1), "y": round(py, 1)}


class Assets:
    def __init__(self):
        self.formats, self.assets, self.ids = [], [], {}

    def format(self, key, fd, w, h):
        if key not in self.ids:
            rid = f"r{len(self.ids) + 1}"
            self.ids[key] = rid
            self.formats.append(f'<format id="{rid}" frameDuration="{rt(fd)}" width="{w}" height="{h}"/>')
        return self.ids[key]

    def asset(self, path: Path, name: str, video: dict | None, duration: float, start: Fraction):
        key = ("asset", str(path))
        if key in self.ids:
            return self.ids[key]
        rid = f"r{len(self.ids) + 1}"
        self.ids[key] = rid
        attrs = [f'id="{rid}"', f"name={quoteattr(name)}", f'start="{rt(start)}"',
                 f'duration="{rt(Fraction(duration).limit_denominator(100000))}"', 'hasAudio="1"',
                 'audioSources="1"', 'audioRate="48000"']
        if video:
            fmt = self.format(("fmt", video["fd"], video["w"], video["h"]), video["fd"], video["w"], video["h"])
            attrs += ['hasVideo="1"', f'format="{fmt}"', f'videoSources="1"']
        self.assets.append(f'<asset {" ".join(attrs)}>'
                           f'<media-rep kind="original-media" src={quoteattr(path.resolve().as_uri())}/></asset>')
        return rid


def export_short(ev: dict, edir: Path, short: dict, out_dir: Path) -> dict:
    plan = plan_short(ev, edir, short)
    fd = frame_duration(plan["fps"])
    total = plan["frames"] * fd
    res = Assets()
    seq_fmt = res.format(("seq",), fd, W, H)

    def video_asset(path: Path, name: str):
        info = probe(path)
        src_fd = frame_duration(info["fps"])
        start = timecode_start(info.get("timecode"), src_fd)
        rid = res.asset(path, name, {"fd": src_fd, "w": info["width"], "h": info["height"]},
                        info["duration"], start)
        return rid, info, src_fd, start

    spine, crops = [], []
    seg_clips = []  # (offset, start, duration) per spine clip, for anchoring connected clips
    for seg in plan["segments"]:
        path = media(ev, ev["sources"][seg["camera"]]["file"])
        rid, info, src_fd, tc0 = video_asset(path, seg["camera"])
        offset = seg["f0"] * fd
        dur = (seg["f1"] - seg["f0"]) * fd
        start = tc0 + round(Fraction(seg["camera_time"]) / src_fd) * src_fd
        scale, pos_x, pos_y, resolve = transform(seg["crop"], info["width"], info["height"])
        seg_clips.append((offset, start, dur))
        crops.append({"t": float(offset), "speaker": seg["speaker"], "camera": seg["camera"], **resolve})
        spine.append(
            f'<asset-clip ref="{rid}" name={quoteattr(seg["speaker"])} offset="{rt(offset)}" '
            f'start="{rt(start)}" duration="{rt(dur)}" srcEnable="video" tcFormat="NDF">'
            f'<adjust-conform type="fit"/>'
            f'<adjust-transform position="{pos_x:.4f} {pos_y:.4f}" scale="{scale:.5f} {scale:.5f}"/>'
            f'{{connected{len(seg_clips) - 1}}}</asset-clip>')

    connected = {i: [] for i in range(len(seg_clips))}

    def anchor(t: Fraction) -> tuple[int, Fraction]:
        """Spine clip containing timeline time t, and t in that clip's local (source) time."""
        for i, (offset, start, dur) in enumerate(seg_clips):
            if offset <= t < offset + dur:
                return i, start + (t - offset)
        offset, start, _ = seg_clips[-1]
        return len(seg_clips) - 1, start + (t - offset)

    mix_rid = res.asset(plan["mix"], f"{plan['panel']} mic mix", None,
                        probe(plan["mix"])["duration"], Fraction(0))
    i, local = anchor(Fraction(0))
    connected[i].append(f'<asset-clip ref="{mix_rid}" lane="-1" name="mic mix" offset="{rt(local)}" '
                        f'start="{rt(Fraction(round(plan["t_in"] * 48000), 48000))}" duration="{rt(total)}" '
                        f'srcEnable="audio"/>')

    broll_note = None
    if plan["broll"]:
        b = plan["broll"]
        rid, info, src_fd, tc0 = video_asset(b["file"], b["file"].stem)
        at = round(Fraction(b["at"]) / fd) * fd
        bdur = round(Fraction(b["dur"]) / fd) * fd
        scale, pos_x, pos_y, _ = transform([0, 0, info["width"], info["height"]], info["width"], info["height"])
        # b-roll fills 9:16 by centre crop: scale to cover, no offset
        i, local = anchor(at)
        connected[i].append(
            f'<asset-clip ref="{rid}" lane="1" name={quoteattr(b["file"].stem)} offset="{rt(local)}" '
            f'start="{rt(tc0 + round(Fraction(b["src_in"]) / src_fd) * src_fd)}" duration="{rt(bdur)}" '
            f'srcEnable="video"><adjust-conform type="fit"/>'
            f'<adjust-transform position="0 0" scale="{scale:.5f} {scale:.5f}"/></asset-clip>')
        broll_note = f"V2 b-roll {b['file'].name} at {b['at']}s for {b['dur']}s"

    body = "".join(c.replace(f"{{connected{i}}}", "".join(connected[i])) for i, c in enumerate(spine))
    name = short["id"]
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.9">
  <resources>
    {chr(10).join("    " + f for f in res.formats).strip()}
    {chr(10).join("    " + a for a in res.assets).strip()}
  </resources>
  <library>
    <event name={quoteattr("shortshog-" + ev["event"])}>
      <project name={quoteattr(name)}>
        <sequence format="{seq_fmt}" duration="{rt(total)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>{body}</spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{name}.fcpxml").write_text(xml)
    write_srt(out_dir / f"{name}.srt", plan["captions"])
    lines = [f"{name}: Resolve Inspector values per V1 clip, if the import drops the transforms",
             "(Video > Transform; Zoom applies to both X and Y; timeline 1080x1920)", ""]
    for c in crops:
        lines.append(f"  at {c['t']:7.2f}s  {c['speaker']:<20} {c['camera']:<6} "
                     f"Zoom {c['zoom']:<8} Position X {c['x']:<8} Y {c['y']}")
    (out_dir / f"{name}.resolve.txt").write_text("\n".join(lines) + "\n")
    return {"id": name, "segments": len(plan["segments"]), "broll": broll_note,
            "captions": len(plan["captions"]), "fps": plan["fps"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event")
    ap.add_argument("--id", action="append", help="only these short ids (repeatable)")
    ap.add_argument("--keep-status", action="store_true", help="don't mark exported shorts edited-in-resolve")
    args = ap.parse_args()

    edir = event_dir(args.event)
    ev = load_event(edir)
    path = edir / "shorts.json"
    shorts = json.loads(path.read_text())
    out_dir = edir / "exports"
    exported = []
    for short in shorts:
        if args.id and short["id"] not in args.id:
            continue
        r = export_short(ev, edir, short, out_dir)
        exported.append(short["id"])
        print(f"{r['id']}: {r['segments']} V1 segments at {r['fps']:.3f} fps, {r['captions']} captions"
              f"{', ' + r['broll'] if r['broll'] else ''} -> exports/{r['id']}.fcpxml")
    if exported and not args.keep_status:
        shutil.copy(path, path.with_suffix(".json.bak"))
        for short in shorts:
            if short["id"] in exported:
                short["status"] = "edited-in-resolve"
        path.write_text(json.dumps(shorts, indent=1) + "\n")


if __name__ == "__main__":
    main()
