"""
Central configuration for the easyEdit pipeline.

Output geometry (background scale, webcam bubble size/position, overlay
placement) is expressed as normalized [0..1] coordinates and pixel constants,
reproduced with ffmpeg / OpenCV. Tune the constants below to match the look you
want; brand/overlay assets are optional and supplied via environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# Project root = the repository folder (parent of this package).
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
ENCODER = os.environ.get("EASYEDIT_ENCODER", "cpu").lower()   # "cpu" | "gpu"
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
FINAL_MAXRATE = os.environ.get("EASYEDIT_MAXRATE", "24M")
FINAL_BUFSIZE = os.environ.get("EASYEDIT_BUFSIZE", "48M")


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
OUT_FPS = 30  # source is often 60fps; 30 is plenty for screen recordings

# ---------------------------------------------------------------------------
# Background layer
#   Scaled up slightly and centred; scaling up pushes the recorder/PDF toolbars
#   off the edges of the frame.
# ---------------------------------------------------------------------------
BG_SCALE = 1.10

# ---------------------------------------------------------------------------
# Webcam layer
#   The source bubble is cropped, scaled and re-placed at a fixed output point.
#   The *input* bubble location varies per recording, so it is auto-detected;
#   the *output* placement below is fixed.
# ---------------------------------------------------------------------------
WEBCAM_OUT_X = 0.890   # normalized centre X
WEBCAM_OUT_Y = 0.203   # normalized centre Y
WEBCAM_OUT_R = 185     # fixed output radius in px
# Crop slightly inside the detected bubble so no background shows at the edge
# (output size is unaffected — the crop is scaled up to WEBCAM_OUT_R).
WEBCAM_MASK_SHRINK = 0.97
# Fallback bubble location (normalized, in source) if detection fails.
WEBCAM_FALLBACK = (0.915, 0.21, 0.07)  # (cx, cy, radius) normalized to width

# ---------------------------------------------------------------------------
# Grey document background removal
#   The document background is a flat neutral grey (~229,229,229); the page is
#   white (255). Low-saturation pixels in this luminance band are treated as
#   background and repainted white — covering side margins and page breaks.
# ---------------------------------------------------------------------------
# Edge-connected background removal (handles light AND dark viewer themes).
BG_MAX_SAT = 26        # max saturation to count a pixel as neutral chrome
BG_GREY_LO = 125       # light/medium viewer canvas band — low enough to catch the
BG_GREY_HI = 250       # mid-grey scrollbar (~170); excludes the white page at 255
BG_DARK_LO = 32        # dark viewer canvas band (~58)
BG_DARK_HI = 124       # (excludes near-black document text < ~32); meets grey band

# ---------------------------------------------------------------------------
# Optional mascot / logo overlay
#   Off by default. Provide an image via EASYEDIT_MASCOT to enable; the overlay
#   is placed at MASCOT_CENTER scaled by MASCOT_SCALE.
# ---------------------------------------------------------------------------
MASCOT_SCALE = 0.20
MASCOT_CENTER = (145.0, 950.0)   # (x, y) centre in output pixels

# ---------------------------------------------------------------------------
# Intro title card
#   Solid background colour, the title in white, and an optional brand line
#   beneath it (set EASYEDIT_BRAND to show it).
# ---------------------------------------------------------------------------
INTRO_BG = os.environ.get("EASYEDIT_INTRO_BG", "0x101418")   # 0xRRGGBB
INTRO_DURATION = 4.0
BRAND_TEXT = os.environ.get("EASYEDIT_BRAND", "")            # "" -> no brand line

# Whether to prepend the intro title card / append the outro. Disabled by
# default — set True (or env EASYEDIT_INTRO / EASYEDIT_OUTRO=1) to enable.
ADD_INTRO = os.environ.get("EASYEDIT_INTRO", "0") == "1"
ADD_OUTRO = os.environ.get("EASYEDIT_OUTRO", "0") == "1"

# ---------------------------------------------------------------------------
# Audio processing (denoise / band-limit / compress / loudness-normalise).
# Disabled by default — the original audio is passed through untouched.
# ---------------------------------------------------------------------------
PROCESS_AUDIO = os.environ.get("EASYEDIT_PROCESS_AUDIO", "0") == "1"

# ---------------------------------------------------------------------------
# Auto-cut dead time: removes silent gaps (scroll-throughs where the presenter
# says nothing, and long pauses). Speech is kept with a little padding so it
# never sounds clipped. Tunable; only gaps longer than MIN_SILENCE are cut.
# ---------------------------------------------------------------------------
AUTOCUT = os.environ.get("EASYEDIT_AUTOCUT", "1") == "1"
AUTOCUT_NOISE_DB = -30      # below this level counts as "silence"
AUTOCUT_MIN_SILENCE = 1.5   # seconds — only cut dead gaps longer than this
AUTOCUT_PAD = 0.30          # seconds of silence kept on each side of speech

# ---------------------------------------------------------------------------
# Asset file paths
#   Only the white background ships with the repo. The mascot overlay and the
#   outro clip are optional and supplied at runtime via environment variables.
# ---------------------------------------------------------------------------
def _env_path(var: str) -> "Optional[str]":
    v = os.environ.get(var, "").strip()
    return v or None


@dataclass
class Assets:
    white_bg: str = field(default_factory=lambda: asset("assets", "white_bg.jpg"))
    mascot: Optional[str] = field(default_factory=lambda: _env_path("EASYEDIT_MASCOT"))
    outro: Optional[str] = field(default_factory=lambda: _env_path("EASYEDIT_OUTRO"))

    def missing(self) -> list:
        # Only the white background is required; mascot/outro are optional.
        return [self.white_bg] if not os.path.exists(self.white_bg) else []


ASSETS = Assets()
