"""
Computer-vision detectors used by the pipeline.

  * detect_webcam_bubble  -> locate the circular Loom webcam in the source frame
  * grey_to_white_mask    -> boolean mask of Word/PDF grey background pixels
  * remove_grey           -> repaint grey pixels white (in place)
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

import cv2
import numpy as np

from . import config as C


@dataclass
class Bubble:
    cx: int          # centre x in source pixels
    cy: int          # centre y in source pixels
    r: int           # radius in source pixels
    detected: bool   # False if we fell back to the preset default


def _sample_frames(video: str, n: int = 5) -> list[np.ndarray]:
    """Grab `n` frames spread across the clip via ffmpeg, decoded with OpenCV."""
    cap = cv2.VideoCapture(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    frames = []
    for i in range(n):
        pos = int(total * (i + 1) / (n + 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ok, fr = cap.read()
        if ok:
            frames.append(fr)
    cap.release()
    return frames


def detect_webcam_bubble(video: str) -> Bubble:
    """Find the webcam circle (top-right quadrant) and return a robust median."""
    frames = _sample_frames(video, 7)
    if not frames:
        h = w = 0
    cands: list[tuple[int, int, int]] = []
    for img in frames:
        h, w = img.shape[:2]
        roi = img[0:int(h * 0.5), int(w * 0.55):w]
        gray = cv2.medianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), 5)
        circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=200,
            param1=100, param2=40,
            minRadius=int(w * 0.05), maxRadius=int(w * 0.10),
        )
        if circles is None:
            continue
        # pick the right-most plausible circle (the webcam hugs the right edge)
        best = max(np.round(circles[0]).astype(int), key=lambda c: c[0])
        cands.append((best[0] + int(w * 0.55), best[1], best[2]))

    if not cands:
        fx, fy, fr = C.WEBCAM_FALLBACK
        return Bubble(int(fx * w), int(fy * h), int(fr * w), detected=False)

    arr = np.array(cands)
    cx, cy, r = (int(np.median(arr[:, 0])), int(np.median(arr[:, 1])),
                 int(np.median(arr[:, 2])))
    return Bubble(cx, cy, r, detected=True)


def remove_background(bgr: np.ndarray) -> np.ndarray:
    """Whiten the viewer 'canvas' around the document page, in place.

    Works for both viewer themes: the light Edge/PDF grey (~229) AND the dark
    Acrobat theme (~58). Strategy: build a mask of neutral *background-coloured*
    pixels (low saturation, in the light-grey OR dark band — excluding the white
    page and near-black text), then flood from the frame border and whiten only
    the connected region. The white page stops the flood, so interior text and
    diagrams are preserved even when they are dark/neutral.

    This also removes the top toolbar and any UI dropdowns (e.g. the dark 'Draw'
    panel) because they are neutral chrome connected to the frame edge.
    """
    h, w = bgr.shape[:2]
    b = bgr[:, :, 0].astype(np.int16)
    g = bgr[:, :, 1].astype(np.int16)
    r = bgr[:, :, 2].astype(np.int16)
    mx = np.maximum(np.maximum(b, g), r)
    mn = np.minimum(np.minimum(b, g), r)
    sat = mx - mn
    neutral = sat <= C.BG_MAX_SAT
    light = (mx >= C.BG_GREY_LO) & (mx <= C.BG_GREY_HI)
    dark = (mx >= C.BG_DARK_LO) & (mx <= C.BG_DARK_HI)
    mask = (neutral & (light | dark)).astype(np.uint8)

    # bridge thin gaps (icons/handles inside the chrome) so the canvas stays one
    # connected region reaching the border
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return bgr

    border = np.concatenate([
        labels[0, :], labels[h - 1, :], labels[:, 0], labels[:, w - 1]
    ])
    border_labels = set(np.unique(border)) - {0}
    if not border_labels:
        return bgr

    keep = np.isin(labels, list(border_labels)).astype(np.uint8)
    # grow slightly so the thin page-border line that sits right at the
    # canvas/page boundary is consumed too (the page has a white margin, so a few
    # px of growth never reaches the document content)
    keep = cv2.dilate(keep, np.ones((7, 7), np.uint8))
    bgr[keep.astype(bool)] = (255, 255, 255)
    return bgr


def remove_ui_panel(bgr: np.ndarray) -> bool:
    """Whiten the light Draw/highlighter dropdown menu (colour swatches, stroke
    preview, thickness slider, toggle) that opens at the top-left of the viewer.

    Detected by its giveaway: a cluster of *multiple distinct hues* (the colour
    swatches) in the top-left corner — the single-colour slide title can't fake
    it. The whitened block extends right only until the gap before the title, so
    the title is never clipped. Call AFTER remove_background. Returns True if a
    menu was found and painted out.
    """
    h, w = bgr.shape[:2]
    cx, cy = int(w * 0.16), int(h * 0.28)
    corner = bgr[:cy, :cx]
    hsv = cv2.cvtColor(corner, cv2.COLOR_BGR2HSV)
    sat = (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 90)
    if int(sat.sum()) < 150:
        return False
    hues = hsv[:, :, 0][sat]
    if len({int(x) for x in (hues // 12)}) < 3:   # need several distinct colours
        return False

    top = bgr[:int(h * 0.42)]
    nonwhite = top.min(axis=2) < 238
    col = nonwhite.sum(axis=0)
    half = int(w * 0.5)
    content = [x for x in range(half) if col[x] > 3]
    if not content:
        return False
    x0 = content[0]
    x1, gap = x0, 0
    for x in range(x0, half):
        if col[x] > 3:
            x1, gap = x, 0
        else:
            gap += 1
            if gap >= 20:        # the gap before the slide title
                break
    region = bgr[:int(h * 0.42), max(0, x0 - 6):x1 + 6]
    yy, _ = np.where(region.min(axis=2) < 238)
    maxy = min(h, int(yy.max()) + 12) if len(yy) else int(h * 0.4)
    bgr[:maxy, max(0, x0 - 6):x1 + 8] = (255, 255, 255)
    return True


