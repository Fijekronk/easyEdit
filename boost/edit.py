"""
ffmpeg / OpenCV editing stages.

Pipeline (replaces the manual Premiere workflow described in the brief):

  1. clean_grey   - repaint Word/PDF grey background white (side margins AND the
                    scrolling page breaks) + normalise fps.  This single CV pass
                    replaces the manual "mask + keyframe every page break" step.
  2. composite    - background layer (scale 112, centred, toolbars cropped off),
                    white fill, enlarged webcam bubble (scale 165, circular),
                    robot mascot overlay, and Podcast-Voice-style audio cleanup.
  3. make_intro   - generated title card (stand-in for SC Intro.mogrt).
  4. assemble     - intro -> body -> outro with cross-fades, exported 1920x1080.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

import cv2
import numpy as np

from . import config as C
from .detect import Bubble, remove_background

def _find_font() -> str:
    """A bold sans-serif font that exists, across Windows / macOS / Linux."""
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",                               # Windows
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",         # macOS
        "/Library/Fonts/Arial Bold.ttf",                             # macOS (older)
        "/System/Library/Fonts/Helvetica.ttc",                       # macOS fallback
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",      # Linux
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


# escape ':' (Windows drive letter) for ffmpeg drawtext's fontfile option
FONT = _find_font().replace("\\", "/").replace(":", "\\:")


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}):\n{' '.join(cmd)}\n{proc.stderr[-2000:]}"
        )


def probe(video: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-show_entries", "format=duration", "-of", "json", video],
        capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    st = data["streams"][0]
    return {
        "w": int(st["width"]),
        "h": int(st["height"]),
        "duration": float(data["format"]["duration"]),
    }


# ---------------------------------------------------------------------------
# Stage 1 - grey background removal (OpenCV streamed through ffmpeg pipes)
# ---------------------------------------------------------------------------
def clean_grey(src: str, dst: str, fps: int = C.OUT_FPS,
               paint_bubble: "Bubble | None" = None,
               workers: "int | None" = None) -> None:
    """Background-removal stage, parallelised across CPU cores.

    The video is split into time-segments, each processed by its own
    `boost.segworker` process, then concatenated. Output is video-only (audio is
    taken from the original source later in `composite`, which keeps A/V in sync
    regardless of how the video segments are cut)."""
    info = probe(src)
    w, h, duration = info["w"], info["h"], info["duration"]

    box = "-"
    if paint_bubble is not None:
        pad = int(paint_bubble.r * 0.45) + 10
        box = "{},{},{},{}".format(
            max(0, paint_bubble.cx - paint_bubble.r - pad), 0,
            min(w, paint_bubble.cx + paint_bubble.r + pad),
            min(h, paint_bubble.cy + paint_bubble.r + pad))

    workers = workers or os.cpu_count() or 1
    workers = max(1, min(workers, int(duration // 8) or 1))   # >= ~8s per segment

    tmp = tempfile.mkdtemp(prefix="boost_seg_")
    seg_dur = duration / workers
    segs, procs = [], []
    for i in range(workers):
        start = i * seg_dur
        dur = "-" if i == workers - 1 else f"{seg_dur}"       # last decodes to EOF
        seg = os.path.join(tmp, f"seg{i:03d}.mp4")
        segs.append(seg)
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "boost.segworker", src, f"{start}", dur,
             str(fps), str(w), str(h), box, seg],
            cwd=C.PROJECT_ROOT))
    for p in procs:
        if p.wait() != 0:
            raise RuntimeError("segworker failed")

    if workers == 1:
        os.replace(segs[0], dst)
        return

    listfile = os.path.join(tmp, "list.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"file '{s.replace(chr(92), '/')}'\n")
    try:
        _run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
              "-i", listfile, "-c", "copy", dst])
    except RuntimeError:
        _run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
              "-i", listfile, *C.video_codec_args(), dst])


# ---------------------------------------------------------------------------
# Stage 2 - composite background + webcam + robot + audio
# ---------------------------------------------------------------------------
def composite(clean: str, cam_src: str, bubble: Bubble, dst: str) -> None:
    """Build the body: background (from `clean`, grey-removed + bubble painted),
    enlarged circular webcam (cropped from `cam_src`, the untouched source),
    robot overlay and Podcast-Voice audio."""
    info = probe(clean)
    sw, sh = info["w"], info["h"]
    OW, OH = C.OUT_W, C.OUT_H

    # --- background layer geometry (scale 112, centred) ---
    bw, bh = round(sw * C.BG_SCALE), round(sh * C.BG_SCALE)
    bx, by = (OW - bw) / 2, (OH - bh) / 2

    # --- webcam: crop slightly inside the detected bubble (drops grey rim),
    #     then scale to a FIXED output diameter so size is consistent ---
    r = max(1, int(bubble.r * C.WEBCAM_MASK_SHRINK))
    cx0 = max(0, bubble.cx - r)
    cy0 = max(0, bubble.cy - r)
    cside = min(2 * r, sw - cx0, sh - cy0)
    D = 2 * C.WEBCAM_OUT_R                       # fixed output diameter

    # circular alpha mask sized to the scaled bubble
    mask = np.zeros((D, D), np.uint8)
    cv2.circle(mask, (D // 2, D // 2), D // 2 - 2, 255, -1)
    mfile = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    cv2.imwrite(mfile, mask)

    wcx, wcy = C.WEBCAM_OUT_X * OW, C.WEBCAM_OUT_Y * OH
    wx, wy = round(wcx - D / 2), round(wcy - D / 2)

    # --- robot geometry ---
    robot = cv2.imread(C.ASSETS.robot, cv2.IMREAD_UNCHANGED)
    rh0, rw0 = robot.shape[:2]
    rw, rh = round(rw0 * C.ROBOT_SCALE), round(rh0 * C.ROBOT_SCALE)
    rcx, rcy = C.ROBOT_CENTER
    rx, ry = round(rcx - rw / 2), round(rcy - rh / 2)

    fc = (
        f"[1:v]scale={OW}:{OH},setsar=1[base0];"
        f"[0:v]setpts=PTS-STARTPTS,scale={bw}:{bh},setsar=1[bgv];"
        f"[base0][bgv]overlay={bx}:{by}[bg];"
        f"[2:v]setpts=PTS-STARTPTS,crop={cside}:{cside}:{cx0}:{cy0},"
        f"scale={D}:{D},setsar=1[camraw];"
        f"[4:v]format=gray,scale={D}:{D}[cmask];"
        f"[camraw][cmask]alphamerge[cam];"
        f"[bg][cam]overlay={wx}:{wy}[withcam];"
        f"[3:v]scale={rw}:{rh}[robot];"
        f"[withcam][robot]overlay={rx}:{ry}[outv];"
        # audio from the ORIGINAL (input 2 = cam_src)
        + (
            # "Podcast Voice"-style cleanup: denoise, band-limit, compress, normalise
            "[2:a]highpass=f=90,lowpass=f=12000,afftdn=nr=12,"
            "acompressor=threshold=-18dB:ratio=3:attack=5:release=120,"
            "loudnorm=I=-16:TP=-1.5:LRA=11[outa]"
            if C.PROCESS_AUDIO else
            # raw audio, untouched (just resampled for the container)
            "[2:a]aresample=44100[outa]"
        )
    )

    _run([
        "ffmpeg", "-v", "error", "-y",
        "-i", clean, "-i", C.ASSETS.white_bg, "-i", cam_src,
        "-i", C.ASSETS.robot, "-i", mfile,
        "-filter_complex", fc, "-map", "[outv]", "-map", "[outa]",
        "-r", str(C.OUT_FPS),
        *C.final_codec_args(), "-c:a", "aac", "-b:a", "192k", dst,
    ])
    os.unlink(mfile)


# ---------------------------------------------------------------------------
# Stage 3 - intro title card (stand-in for SC Intro.mogrt)
# ---------------------------------------------------------------------------
def make_intro(title: str, dst: str, duration: float = C.INTRO_DURATION) -> None:
    """Title card matching the SC Intro / SCI_FINAL look: teal background, the
    lesson topic in white, "studyclix" beneath."""
    OW, OH = C.OUT_W, C.OUT_H

    def esc(s: str) -> str:
        return (s.replace("\\", r"\\").replace(":", r"\:")
                 .replace("'", "’").replace("%", r"\%"))

    # shrink the font if the title is long so it always fits on one line
    fontsize = 96 if len(title) <= 28 else max(54, int(96 * 28 / len(title)))
    safe = esc(title)

    fc = (
        f"color=c={C.INTRO_TEAL}:s={OW}x{OH}:r={C.OUT_FPS}:d={duration}[bg];"
        f"[bg]drawtext=fontfile='{FONT}':text='{safe}':fontcolor=white:"
        f"fontsize={fontsize}:x=(w-text_w)/2:y=h*0.40[t1];"
        f"[t1]drawtext=fontfile='{FONT}':text='studyclix':fontcolor=white:"
        f"fontsize=58:x=(w-text_w)/2:y=h*0.56[t2];"
        f"[t2]fade=t=in:st=0:d=0.5,fade=t=out:st={duration-0.6}:d=0.6[outv]"
    )
    _run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration}",
        "-filter_complex", fc, "-map", "[outv]", "-map", "0:a",
        *C.video_codec_args(), "-c:a", "aac", dst,
    ])


def has_audio(video: str) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", video],
        capture_output=True, text=True,
    ).stdout.strip()
    return bool(out)


def _normalize(src: str, dst: str) -> None:
    """Conform any clip to OUT_W x OUT_H @ OUT_FPS, always with a stereo track."""
    vf = (f"scale={C.OUT_W}:{C.OUT_H}:force_original_aspect_ratio=decrease,"
          f"pad={C.OUT_W}:{C.OUT_H}:(ow-iw)/2:(oh-ih)/2:white,"
          f"fps={C.OUT_FPS},setsar=1")
    if has_audio(src):
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", vf,
               "-af", "aformat=sample_rates=44100:channel_layouts=stereo",
               "-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", src,
               "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    cmd += [*C.video_codec_args(), "-c:a", "aac", dst]
    _run(cmd)


# ---------------------------------------------------------------------------
# Stage 4 - assemble intro -> body -> outro with cross-fades
# ---------------------------------------------------------------------------
def assemble(intro: str, body: str, outro_src: str, dst: str, xfade: float = 0.6) -> None:
    tmp = tempfile.mkdtemp(prefix="boost_")
    outro = os.path.join(tmp, "outro_norm.mp4")
    _normalize(outro_src, outro)

    di = probe(intro)["duration"]
    db = probe(body)["duration"]
    o1 = di - xfade            # intro->body transition start
    o2 = o1 + db - xfade       # body->outro transition start

    fc = (
        f"[0:v][1:v]xfade=transition=fade:duration={xfade}:offset={o1}[v01];"
        f"[v01][2:v]xfade=transition=fade:duration={xfade}:offset={o2}[outv];"
        f"[0:a][1:a]acrossfade=d={xfade}[a01];"
        f"[a01][2:a]acrossfade=d={xfade}[outa]"
    )
    _run([
        "ffmpeg", "-v", "error", "-y",
        "-i", intro, "-i", body, "-i", outro,
        "-filter_complex", fc, "-map", "[outv]", "-map", "[outa]",
        *C.final_codec_args(), "-c:a", "aac", "-b:a", "192k",
        "-r", str(C.OUT_FPS), dst,
    ])


# ---------------------------------------------------------------------------
# Auto-cut dead time (silent scroll-throughs / long pauses)
# ---------------------------------------------------------------------------
def _detect_silences(src: str, noise_db: float, min_sil: float):
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", src, "-af",
         f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"],
        capture_output=True, text=True)
    t = p.stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", t)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[0-9.]+)", t)]
    return starts, ends


def autocut(src: str, dst: str, noise_db: float = None,
            min_sil: float = None, pad: float = None) -> bool:
    """Remove silent gaps (dead scroll time, long pauses), keeping `pad` seconds
    of silence around speech. Cuts video AND audio together. Returns True if any
    cut was made (dst written), False if nothing to cut."""
    noise_db = C.AUTOCUT_NOISE_DB if noise_db is None else noise_db
    min_sil = C.AUTOCUT_MIN_SILENCE if min_sil is None else min_sil
    pad = C.AUTOCUT_PAD if pad is None else pad

    dur = probe(src)["duration"]
    starts, ends = _detect_silences(src, noise_db, min_sil)

    removed = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else dur
        a, b = s + pad, e - pad
        if b - a > 0.2:
            removed.append((a, b))
    if not removed:
        return False

    keep, cur = [], 0.0
    for a, b in sorted(removed):
        if a > cur:
            keep.append((cur, a))
        cur = max(cur, b)
    if cur < dur:
        keep.append((cur, dur))
    keep = [(a, b) for a, b in keep if b - a > 0.1]
    if not keep:
        return False

    parts, labels = [], ""
    for i, (a, b) in enumerate(keep):
        parts.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}]")
        labels += f"[v{i}][a{i}]"
    parts.append(f"{labels}concat=n={len(keep)}:v=1:a=1[vo][ao]")
    _run(["ffmpeg", "-v", "error", "-y", "-i", src,
          "-filter_complex", ";".join(parts), "-map", "[vo]", "-map", "[ao]",
          *C.video_codec_args(), "-c:a", "aac", dst])
    return True
