# easyEdit — automated screen-recording editor

Turns a raw screen-recording lesson (screen + webcam baked into one file) into a
clean **1920×1080** video — no NLE required, using **ffmpeg + OpenCV**. Ships as
a CLI and a small FastAPI web app with an in-browser visual editor.

## Pipeline

| Stage | What it does |
|---|---|
| `autocut` | removes silent dead time (scroll-throughs, long pauses), keeping padding around speech |
| `detect` | auto-locates the circular webcam bubble in the source frame |
| `clean_grey` | repaints the Word/PDF grey background white — side margins **and** scrolling page breaks — in one per-frame OpenCV pass (no manual masking/keyframing) |
| `composite` | scales & centres the background, drops toolbars off-frame, re-places the enlarged circular webcam at a fixed spot, optional mascot overlay, optional audio cleanup |
| `make_intro` | generated title card (solid colour + title + optional brand line) |
| `assemble` | intro → body → outro with cross-fades, exported 1920×1080 |

The grey-removal pass is edge-connected, so it keys out the viewer canvas (light
**and** dark themes) while the white page stops the flood — interior text and
diagrams are preserved.

## Install

```bash
pip install -r requirements.txt
# ffmpeg + ffprobe must be on PATH
```

## CLI

```bash
python -m easyedit.cli "raw.mp4" --title "My Title" --out ./output
python -m easyedit.cli "raw.mp4" --title "Quick test" --out ./output --clip 20   # first 20s
python -m easyedit.cli "raw.mp4" --title "No branding" --out ./output --no-intro --no-outro
```

## Web app

```bash
python -m uvicorn easyedit.web.app:app --port 8000
# open http://127.0.0.1:8000/  → upload mp4 + title → progress → download
```

**Queue:** one job at a time, using *all* CPU cores. New jobs wait and the UI
shows their position (`queue_position` from `/api/jobs/{id}`).

### Visual editor

After a render completes, the page opens a visual editor on the result:

- **Timeline** with a ruler, zoom, draggable **playhead**, and a video track.
- **Trim** by dragging the handles; **cut out** spans with the ✂ tool.
- **Text / image overlays** — added at the playhead, dragged and resized directly
  on the video; their clip on the timeline sets when and how long they appear.

The editor posts an edit spec (explicit 1920×1080 pixel coords + asset files) to
`/api/jobs/{id}/edit`; `easyedit/editor.py` renders overlays first, then applies
trim/cuts. Results can be edited again.

## Layout

```
easyedit/            python package (the pipeline)
  config.py          constants (geometry, encoder, intro/outro flags, asset paths)
  detect.py          webcam-bubble detection + edge-connected background removal
  edit.py            ffmpeg/OpenCV stages: clean_grey, composite, intro, assemble
  segworker.py       per-segment background-removal worker (one process per core)
  editor.py          in-browser edit renderer (trim/cut/text/image)
  pipeline.py        end-to-end orchestration with progress callbacks
  cli.py             command-line entry point
  web/               FastAPI app + single-page visual editor
assets/              white_bg.jpg (the only bundled asset)
scripts/             dev tools (run_batch.py)
```

## Configuration

Behaviour is driven by environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `EASYEDIT_ENCODER` | `cpu` | `cpu` (libx264) or `gpu` (NVIDIA NVENC) |
| `EASYEDIT_INTRO` / `EASYEDIT_OUTRO` | `0` | prepend intro card / append outro clip |
| `EASYEDIT_BRAND` | — | brand line shown under the intro title |
| `EASYEDIT_INTRO_BG` | `0x101418` | intro background colour (`0xRRGGBB`) |
| `EASYEDIT_MASCOT` | — | path to an optional mascot/logo overlay image |
| `EASYEDIT_OUTRO` | — | path to an optional outro clip |
| `EASYEDIT_PROCESS_AUDIO` | `0` | denoise + band-limit + compress + loudness-normalise |
| `EASYEDIT_AUTOCUT` | `1` | cut silent dead time |
| `EASYEDIT_MAXRATE` / `EASYEDIT_BUFSIZE` | `24M` / `48M` | final-export H.264 rate cap |

## Notes

- **Encoder toggle (CPU/GPU):** `POST /api/encoder {"encoder":"gpu"|"cpu"}` or the
  UI buttons. GPU (NVENC) accelerates the main full-length encodes; the segmented
  background pass stays multi-core CPU (its encode is negligible and dozens of
  parallel NVENC sessions would exceed the GPU session limit).
- **Overlays are optional.** Without `EASYEDIT_MASCOT` / `EASYEDIT_OUTRO`, the
  mascot and outro stages are skipped; only `assets/white_bg.jpg` is required.
- **Final export** is high-bitrate H.264 (libx264 `crf 17`, or `h264_nvenc`),
  capped at `EASYEDIT_MAXRATE`. Intermediate/segment encodes stay fast.
