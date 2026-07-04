"""
Per-segment background-removal worker (one OS process per CPU core).

Run as:  python -m boost.segworker <src> <start> <dur|-> <fps> <w> <h> <box|-> <out>
  box = "x0,y0,x1,y1" white-fill rectangle (original webcam), or "-" for none.

Launched by edit.clean_grey to parallelise the (single-threaded) OpenCV pass
across cores without relying on multiprocessing spawn semantics — robust under
uvicorn on Windows.
"""
from __future__ import annotations

import subprocess
import sys

import numpy as np

from .detect import remove_background, remove_ui_panel


def main(argv: list[str]) -> int:
    src, start, dur, fps, w, h, box, out = argv
    fps_i, w_i, h_i = int(fps), int(w), int(h)
    box_t = None if box == "-" else tuple(int(v) for v in box.split(","))

    dec_cmd = ["ffmpeg", "-v", "error", "-ss", start, "-i", src]
    if dur != "-":
        dec_cmd += ["-t", dur]
    dec_cmd += ["-r", fps, "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE)
    enc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w_i}x{h_i}", "-r", fps,
         "-i", "pipe:0", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", out],
        stdin=subprocess.PIPE,
    )
    frame_bytes = w_i * h_i * 3
    while True:
        buf = dec.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(h_i, w_i, 3).copy()
        remove_background(frame)
        remove_ui_panel(frame)
        if box_t is not None:
            x0, y0, x1, y1 = box_t
            frame[y0:y1, x0:x1] = (255, 255, 255)
        enc.stdin.write(frame.tobytes())
    enc.stdin.close()
    dec.wait()
    return enc.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
