"""End-to-end orchestration of the easyEdit editing pipeline."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from . import config as C
from . import edit
from .detect import detect_webcam_bubble

Progress = Callable[[str, float], None]   # (message, fraction 0..1)


@dataclass
class Result:
    output: str
    bubble_detected: bool


def run_pipeline(
    src: str,
    title: str,
    out_dir: str,
    *,
    work_dir: Optional[str] = None,
    progress: Optional[Progress] = None,
    make_intro: Optional[bool] = None,
    make_outro: Optional[bool] = None,
) -> Result:
    if make_intro is None:
        make_intro = C.ADD_INTRO
    if make_outro is None:
        make_outro = C.ADD_OUTRO
    # the outro is optional — only append it if an outro clip is actually provided
    if make_outro and not (C.ASSETS.outro and os.path.exists(C.ASSETS.outro)):
        make_outro = False

    def report(msg: str, frac: float) -> None:
        if progress:
            progress(msg, frac)

    os.makedirs(out_dir, exist_ok=True)
    work = work_dir or tempfile.mkdtemp(prefix="easyedit_work_")
    os.makedirs(work, exist_ok=True)

    clean = os.path.join(work, "clean.mp4")
    body = os.path.join(work, "body.mp4")
    cut = os.path.join(work, "cut.mp4")
    intro = os.path.join(work, "intro.mp4")
    safe_name = "".join(c for c in title if c.isalnum() or c in " -_").strip() or "easyedit"
    final = os.path.join(out_dir, f"{safe_name}.mp4")

    # Optional pre-step: cut out silent dead time (scroll-throughs, long pauses).
    # Shortens the source so the rest of the pipeline also processes less.
    src_proc = src
    if C.AUTOCUT:
        report("Cutting silent dead time…", 0.03)
        try:
            if edit.autocut(src, cut):
                src_proc = cut
        except Exception:  # noqa: BLE001 — never fail the whole job on autocut
            src_proc = src

    report("Detecting webcam bubble…", 0.10)
    bubble = detect_webcam_bubble(src_proc)

    report("Removing grey background + masking page breaks…", 0.18)
    edit.clean_grey(src_proc, clean, paint_bubble=bubble)

    report("Compositing layers…", 0.50)
    edit.composite(clean, src_proc, bubble, body)

    if make_intro or make_outro:
        report("Building intro / outro…", 0.80)
        parts_body = body
        if make_intro:
            edit.make_intro(title, intro)
        if make_intro and make_outro:
            edit.assemble(intro, parts_body, C.ASSETS.outro, final)
        elif make_intro:
            edit.assemble(intro, parts_body, parts_body, final)  # rare path
        else:
            # outro only
            tmp_intro = os.path.join(work, "blank_intro.mp4")
            edit.make_intro(title, tmp_intro, duration=0.1)
            edit.assemble(tmp_intro, parts_body, C.ASSETS.outro, final)
    else:
        os.replace(body, final)

    report("Done.", 1.0)
    return Result(output=final, bubble_detected=bubble.detected)
