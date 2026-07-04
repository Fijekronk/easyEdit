"""
Minimal in-browser editor render backend.

Takes a rendered Boost video plus an "edit spec" and produces a new video.
Supported operations (a minimal montage):

  * trim          keep only [start, end]
  * cuts          remove one or more [start, end] segments from the middle
  * texts         burn-in text overlays (timed, positioned)
  * images        overlay an uploaded image (timed, positioned, scaled)
  * audios        mix in / replace with uploaded audio (timed, volume)

Render order: overlays first (so overlay times match the timeline the user
scrubs in the preview), then trim/cuts last (which simply drop spans, taking any
overlays inside them with them).

Spec shape (all times in seconds, on the *original* rendered timeline):
{
  "trim":  {"start": 0, "end": null},          # null end = to the end
  "cuts":  [{"start": 10, "end": 15}, ...],
  "mute_original": false,
  "texts":  [{"text","start","end","pos","size","color"}],
  "images": [{"id","start","end","pos","scale"}],
  "audios": [{"id","start","volume"}],
}
`pos` is one of: tl tc tr ml c mr bl bc br.  `id` keys into `asset_paths`.
"""
from __future__ import annotations

import os
import tempfile
from typing import Callable, Dict, List, Optional

from . import config as C
from .edit import FONT, _run, probe

Progress = Callable[[str, float], None]
MARGIN = 40


def _esc_text(s: str) -> str:
    return (s.replace("\\", r"\\").replace(":", r"\:")
             .replace("'", "’").replace("%", r"\%").replace("\n", " "))


def _text_xy(pos: str) -> tuple[str, str]:
    x = {"l": str(MARGIN), "c": "(w-text_w)/2", "r": f"w-text_w-{MARGIN}"}
    y = {"t": str(MARGIN), "m": "(h-text_h)/2", "b": f"h-text_h-{MARGIN}"}
    v, h = (pos[0], pos[1]) if len(pos) == 2 else ("m", "c")
    return x[h], y[v]


def _overlay_xy(pos: str) -> tuple[str, str]:
    x = {"l": str(MARGIN), "c": "(W-w)/2", "r": f"W-w-{MARGIN}"}
    y = {"t": str(MARGIN), "m": "(H-h)/2", "b": f"H-h-{MARGIN}"}
    v, h = (pos[0], pos[1]) if len(pos) == 2 else ("m", "c")
    return x[h], y[v]


def _keep_intervals(trim: dict, cuts: List[dict], duration: float) -> List[tuple]:
    start = float(trim.get("start") or 0)
    end = trim.get("end")
    end = float(end) if end not in (None, "") else duration
    start = max(0.0, start)
    end = min(duration, end)
    intervals = [(start, end)]
    for cut in sorted(cuts, key=lambda c: float(c["start"])):
        cs, ce = float(cut["start"]), float(cut["end"])
        new: List[tuple] = []
        for (s, e) in intervals:
            if ce <= s or cs >= e:        # no overlap
                new.append((s, e))
            else:
                if cs > s:
                    new.append((s, min(cs, e)))
                if ce < e:
                    new.append((max(ce, s), e))
        intervals = new
    return [(s, e) for (s, e) in intervals if e - s > 0.05]


def _apply_overlays(src: str, spec: dict, assets: Dict[str, str], dst: str) -> None:
    texts = spec.get("texts", [])
    images = spec.get("images", [])
    audios = spec.get("audios", [])
    mute = bool(spec.get("mute_original"))

    inputs = ["-i", src]
    img_index: List[int] = []
    for img in images:
        path = assets.get(img.get("id", ""))
        if path:
            inputs += ["-i", path]
            img_index.append(len(img_index))
    aud_index: List[int] = []
    for aud in audios:
        path = assets.get(aud.get("id", ""))
        if path:
            inputs += ["-i", path]
            aud_index.append(len(aud_index))

    n_img = len(img_index)
    parts: List[str] = []
    cur = "[0:v]"

    # image overlays (input streams 1 .. n_img). Position/size are explicit
    # pixel coords on the 1920x1080 frame (from the visual editor); fall back to
    # a position preset + scale if not given.
    for i, img in enumerate([im for im in images if assets.get(im.get("id", ""))]):
        k = 1 + i
        s, e = float(img.get("start", 0)), float(img.get("end", 1e9))
        if "w" in img and img.get("w"):
            parts.append(f"[{k}:v]scale={int(img['w'])}:-1[img{i}]")
        else:
            parts.append(f"[{k}:v]scale=iw*{float(img.get('scale', 1.0))}:-1[img{i}]")
        if "x" in img and "y" in img:
            x, y = str(int(img["x"])), str(int(img["y"]))
        else:
            x, y = _overlay_xy(img.get("pos", "tl"))
        out = f"[ov{i}]"
        parts.append(f"{cur}[img{i}]overlay=x={x}:y={y}:"
                     f"enable='between(t,{s},{e})'{out}")
        cur = out

    # text overlays
    for j, t in enumerate(texts):
        s, e = float(t.get("start", 0)), float(t.get("end", 1e9))
        if "x" in t and "y" in t:
            x, y = str(int(t["x"])), str(int(t["y"]))
        else:
            x, y = _text_xy(t.get("pos", "bc"))
        size = int(t.get("size", 54))
        color = t.get("color", "white")
        txt = _esc_text(str(t.get("text", "")))
        out = f"[tx{j}]"
        parts.append(f"{cur}drawtext=fontfile='{FONT}':text='{txt}':"
                     f"fontcolor={color}:fontsize={size}:x={x}:y={y}:"
                     f"borderw=3:bordercolor=black@0.6:"
                     f"enable='between(t,{s},{e})'{out}")
        cur = out

    if cur == "[0:v]":
        parts.append("[0:v]copy[vout]")
    else:
        parts.append(f"{cur}copy[vout]")

    # audio
    a_streams: List[str] = []
    if not mute:
        a_streams.append("[0:a]")
    for i, aud in enumerate([a for a in audios if assets.get(a.get("id", ""))]):
        k = 1 + n_img + i
        delay = int(float(aud.get("start", 0)) * 1000)
        vol = float(aud.get("volume", 1.0))
        lbl = f"[aa{i}]"
        parts.append(f"[{k}:a]adelay={delay}|{delay},volume={vol}{lbl}")
        a_streams.append(lbl)
    if not a_streams:
        parts.append("anullsrc=r=44100:cl=stereo[aout]")
    elif len(a_streams) == 1:
        parts.append(f"{a_streams[0]}aresample=44100[aout]")
    else:
        parts.append(f"{''.join(a_streams)}amix=inputs={len(a_streams)}:"
                     f"normalize=0:duration=first[aout]")

    fc = ";".join(parts)
    _run([
        "ffmpeg", "-v", "error", "-y", *inputs,
        "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
        *C.final_codec_args(), "-c:a", "aac", "-b:a", "192k", "-shortest", dst,
    ])


def _apply_trim_cuts(src: str, intervals: List[tuple], dst: str) -> None:
    parts, labels = [], ""
    for i, (s, e) in enumerate(intervals):
        parts.append(f"[0:v]trim={s}:{e},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS[a{i}]")
        labels += f"[v{i}][a{i}]"
    parts.append(f"{labels}concat=n={len(intervals)}:v=1:a=1[vout][aout]")
    _run([
        "ffmpeg", "-v", "error", "-y", "-i", src,
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
        *C.final_codec_args(), "-c:a", "aac", "-b:a", "192k", dst,
    ])


def render_edit(src: str, spec: dict, assets: Dict[str, str], dst: str,
                progress: Optional[Progress] = None) -> None:
    def report(m: str, f: float) -> None:
        if progress:
            progress(m, f)

    work_dir = tempfile.mkdtemp(prefix="boost_edit_")
    has_overlays = bool(spec.get("texts") or spec.get("images")
                        or spec.get("audios") or spec.get("mute_original"))

    cur = src
    if has_overlays:
        report("Applying overlays…", 0.2)
        ov = os.path.join(work_dir, "overlays.mp4")
        _apply_overlays(src, spec, assets, ov)
        cur = ov

    duration = probe(cur)["duration"]
    intervals = _keep_intervals(spec.get("trim", {}), spec.get("cuts", []), duration)
    full = (len(intervals) == 1 and intervals[0][0] <= 0.05
            and intervals[0][1] >= duration - 0.05)

    if intervals and not full:
        report("Trimming / cutting…", 0.6)
        _apply_trim_cuts(cur, intervals, dst)
    else:
        report("Finalising…", 0.6)
        if cur != src:
            os.replace(cur, dst)
        else:
            # nothing changed — just re-mux a copy
            _run(["ffmpeg", "-v", "error", "-y", "-i", src,
                  "-c", "copy", dst])
    report("Done.", 1.0)
