"""
Central configuration for the Boost editing pipeline.

All geometric constants were decoded from the two Premiere Pro presets that the
manual workflow uses:
    Premiere Pro Presets - JC Boost/JC Lesson - Background.prfpset.xml
    Premiere Pro Presets - JC Boost/JC Lesson - Webcam.prfpset.xml

The presets store AE.ADBE Motion (Position / Scale / Anchor) values in
normalized [0..1] coordinates, which we reproduce here with ffmpeg/OpenCV.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Project root = the "black camel project" folder (parent of this package).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def asset(*parts: str) -> str:
    return os.path.join(PROJECT_ROOT, *parts)


# ---------------------------------------------------------------------------
# Video encoder: switchable CPU (libx264) <-> GPU (NVIDIA NVENC) at runtime.
#   Affects the main full-length encodes (composite / intro / assemble / editor).
#   The segmented background pass always encodes its tiny segments on CPU — its
#   encode time is negligible and 24 parallel NVENC sessions would exceed the
#   GPU's session limit. GPU mode mainly speeds the big single-stream encodes.
# ---------------------------------------------------------------------------
ENCODER = os.environ.get("BOOST_ENCODER", "cpu").lower()   # "cpu" | "gpu"
GPU_VCODEC = "h264_nvenc"


def video_codec_args(mode: "str | None" = None) -> list:
    """Fast args for INTERMEDIATE encodes (segments / plates that get re-encoded)."""
    m = (mode or ENCODER or "cpu").lower()
    if m == "gpu":
        return ["-c:v", GPU_VCODEC, "-preset", "p5", "-rc", "vbr",
                "-cq", "23", "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p"]


# High-bitrate H.264 for the FINAL user-facing export (crisp, generous bitrate).
FINAL_MAXRATE = os.environ.get("BOOST_MAXRATE", "24M")
FINAL_BUFSIZE = os.environ.get("BOOST_BUFSIZE", "48M")


def final_codec_args(mode: "str | None" = None) -> list:
    """High-quality / high-bitrate H.264 args for the exported video."""
    m = (mode or ENCODER or "cpu").lower()
    if m == "gpu":
        return ["-c:v", GPU_VCODEC, "-preset", "p6", "-rc", "vbr",
                "-cq", "19", "-b:v", "12M",
                "-maxrate", FINAL_MAXRATE, "-bufsize", FINAL_BUFSIZE,
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "17",
            "-maxrate", FINAL_MAXRATE, "-bufsize", FINAL_BUFSIZE,
            "-pix_fmt", "yuv420p"]


# ---------------------------------------------------------------------------
# Output sequence
# ---------------------------------------------------------------------------
OUT_W = 1920
OUT_H = 1080
OUT_FPS = 30  # source is 60fps; 30 is plenty for screen lessons and halves work

# ---------------------------------------------------------------------------
# Background layer (JC Lesson - Background preset)
#   Scale 112%, centered. Scaling up pushes the Loom/PDF toolbars off the edges.
# ---------------------------------------------------------------------------
BG_SCALE = 1.10   # matched to reference edits (Ireland raw→edited title ratio ≈1.105)

# ---------------------------------------------------------------------------
# Webcam layer (JC Lesson - Webcam preset)
#   Source bubble is cropped, scaled 165% and re-placed at a fixed output point.
#   The *input* bubble location varies per recording, so it is auto-detected;
#   the *output* placement below is fixed (preset Position 0.891 : 0.305).
# ---------------------------------------------------------------------------
# Webcam bubble is placed at a FIXED output size + position (matched to the
# client's reference edits), independent of the detected source bubble size.
WEBCAM_OUT_X = 0.890   # normalized centre X (nudged tighter to the right edge)
WEBCAM_OUT_Y = 0.203   # normalized centre Y
WEBCAM_OUT_R = 185     # fixed output radius in px (reference ≈ 175-185)
# Crop slightly inside the detected bubble so no background grey shows at the
# edge (output size is unaffected — the crop is scaled up to WEBCAM_OUT_R).
WEBCAM_MASK_SHRINK = 0.97
# Fallback bubble location (normalized, in source) if detection fails.
WEBCAM_FALLBACK = (0.915, 0.21, 0.07)  # (cx, cy, radius) normalized to width

# ---------------------------------------------------------------------------
# Grey Word/PDF background removal
#   The document background is a flat neutral grey (~229,229,229); the page is
#   white (255). We treat low-saturation pixels in this luminance band as grey
#   and repaint them white. Covers both side margins and scrolling page breaks.
# ---------------------------------------------------------------------------
# Edge-connected background removal (handles light AND dark viewer themes).
BG_MAX_SAT = 26        # max saturation to count a pixel as neutral chrome
BG_GREY_LO = 125       # light/medium viewer canvas band — low enough to catch the
BG_GREY_HI = 250       # mid-grey scrollbar (~170); excludes the white page at 255
BG_DARK_LO = 32        # dark viewer canvas band (Acrobat theme ~58)
BG_DARK_HI = 124       # (excludes near-black document text < ~32); meets grey band

# ---------------------------------------------------------------------------
# Robot mascot overlay (pose1-smile.png), Premiere scale 20, position 145 x 950.
#   Premiere position is the clip *centre* in sequence pixels.
# ---------------------------------------------------------------------------
ROBOT_SCALE = 0.20
ROBOT_CENTER = (145.0, 950.0)   # (x, y) centre in output pixels — bottom-left
ROBOT_CORNER = "bottom-left"    # confirmed left by reference (SCI_FINAL)

# ---------------------------------------------------------------------------
# Intro title card (matches SC Intro / SCI_FINAL look)
#   Teal background, white lesson topic, "studyclix" beneath.
# ---------------------------------------------------------------------------
INTRO_TEAL = "0x0AAE9F"     # RGB(10,174,159) brand teal
INTRO_DURATION = 4.0

# Whether to prepend the intro title card / append the outro. Disabled for now
# (requested) — set True (or env BOOST_INTRO / BOOST_OUTRO=1) to re-enable.
ADD_INTRO = os.environ.get("BOOST_INTRO", "0") == "1"
ADD_OUTRO = os.environ.get("BOOST_OUTRO", "0") == "1"

# ---------------------------------------------------------------------------
# Audio: "Podcast Voice"-style processing. Disabled for now (requested) — the
# original audio is passed through untouched.
# ---------------------------------------------------------------------------
PROCESS_AUDIO = os.environ.get("BOOST_PROCESS_AUDIO", "0") == "1"

# ---------------------------------------------------------------------------
# Auto-cut dead time: removes silent gaps (scroll-throughs where the presenter
# says nothing, and long pauses). Speech is kept with a little padding so it
# never sounds clipped. Tunable; only gaps longer than MIN_SILENCE are cut.
# ---------------------------------------------------------------------------
AUTOCUT = os.environ.get("BOOST_AUTOCUT", "1") == "1"
AUTOCUT_NOISE_DB = -30      # below this level counts as "silence"
AUTOCUT_MIN_SILENCE = 1.5   # seconds — only cut dead gaps longer than this
AUTOCUT_PAD = 0.30          # seconds of silence kept on each side of speech

# ---------------------------------------------------------------------------
# Asset file paths
# ---------------------------------------------------------------------------
@dataclass
class Assets:
    white_bg: str = field(default_factory=lambda: asset("assets", "White Background.jpg"))
    robot: str = field(default_factory=lambda: asset("assets", "pose1-smile.png"))
    outro: str = field(default_factory=lambda: asset("assets", "Outro Fade with logo.mov"))
    # SC Intro.mogrt is Premiere-only; we render a title card instead (see edit.py)
    intro_logo: str = field(default_factory=lambda: asset("assets", "Studyclix_robot.png"))

    def missing(self) -> list[str]:
        out = []
        for name in ("white_bg", "robot", "outro"):
            p = getattr(self, name)
            if not os.path.exists(p):
                out.append(p)
        return out


ASSETS = Assets()
