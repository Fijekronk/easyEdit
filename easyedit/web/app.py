"""
FastAPI web front-end for the easyEdit editing pipeline.

Upload a raw screen-recording mp4 + a title -> the job is queued, processed by the pipeline,
and the finished 1920x1080 video can be downloaded.  A single background worker
processes jobs sequentially (video editing is CPU-heavy; one at a time is right
for a test deployment).

Run with:
    uvicorn easyedit.web.app:app --reload --port 8000
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, Optional

import json

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import subprocess

from .. import config as C
from ..editor import render_edit
from ..pipeline import run_pipeline


def _gpu_available() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True).stdout
        return C.GPU_VCODEC in out
    except Exception:  # noqa: BLE001
        return False


_GPU_OK = _gpu_available()

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DATA = os.path.join(tempfile.gettempdir(), "easyedit_jobs")
os.makedirs(DATA, exist_ok=True)

app = FastAPI(title="easyEdit Video Editor")
# ONE worker: each video is processed on all CPU cores, jobs run one-by-one.
_executor = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()


@dataclass
class Job:
    id: str
    title: str
    kind: str = "render"          # render | edit
    status: str = "queued"        # queued | running | done | error
    progress: float = 0.0
    message: str = "Queued…"
    output: Optional[str] = None
    error: Optional[str] = None
    bubble_detected: bool = False


JOBS: Dict[str, Job] = {}
JOB_ORDER: list[str] = []         # submission order, for queue position
REGISTRY = os.path.join(DATA, "registry.json")


def _save_registry() -> None:
    """Persist render jobs to disk so the list survives a server restart."""
    try:
        rows = []
        for jid in JOB_ORDER:
            j = JOBS.get(jid)
            if not j or j.kind != "render":
                continue
            rows.append({"id": j.id, "title": j.title, "status": j.status,
                         "output": j.output, "bubble_detected": j.bubble_detected})
        with open(REGISTRY, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    except Exception:  # noqa: BLE001
        pass


def _load_registry() -> None:
    """On startup, restore finished render jobs whose output file still exists."""
    if not os.path.exists(REGISTRY):
        return
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:  # noqa: BLE001
        return
    for r in rows:
        out = r.get("output")
        done = r.get("status") == "done" and out and os.path.exists(out)
        JOBS[r["id"]] = Job(
            id=r["id"], title=r.get("title", "lesson"), kind="render",
            status="done" if done else "error",
            progress=1.0 if done else 0.0,
            message="Done." if done else "Interrupted (server restarted).",
            output=out if done else None,
            error=None if done else "interrupted",
            bubble_detected=r.get("bubble_detected", False),
        )
        JOB_ORDER.append(r["id"])


def _queue_position(job_id: str) -> int:
    """How many jobs are ahead of this one still waiting/running (0 = next/now)."""
    pos = 0
    for jid in JOB_ORDER:
        if jid == job_id:
            break
        j = JOBS.get(jid)
        if j and j.status in ("queued", "running"):
            pos += 1
    return pos


def _process(job_id: str, src_path: str) -> None:
    job = JOBS[job_id]
    job.status = "running"
    out_dir = os.path.join(DATA, job_id, "out")

    def progress(msg: str, frac: float) -> None:
        job.message = msg
        job.progress = frac

    try:
        res = run_pipeline(src_path, job.title, out_dir, progress=progress)
        job.output = res.output
        job.bubble_detected = res.bubble_detected
        job.status = "done"
        job.message = "Done."
        job.progress = 1.0
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = f"{exc}"
        job.message = "Failed."
        traceback.print_exc()
    _save_registry()


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...),
                     title: str = Form("")) -> dict:
    # title is optional now (no intro); default to the uploaded file name
    title = title.strip() or os.path.splitext(file.filename or "")[0] or "lesson"
    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(DATA, job_id)
    os.makedirs(job_dir, exist_ok=True)
    src_path = os.path.join(job_dir, "input.mp4")
    with open(src_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    with _lock:
        JOBS[job_id] = Job(id=job_id, title=title.strip(), kind="render")
        JOB_ORDER.append(job_id)
    _save_registry()
    _executor.submit(_process, job_id, src_path)
    return {"id": job_id}


@app.get("/api/jobs")
async def list_jobs() -> list:
    """All render jobs in submission order — lets the UI restore the list on
    refresh (the server is the source of truth)."""
    out = []
    for jid in JOB_ORDER:
        j = JOBS.get(jid)
        if not j or j.kind != "render":
            continue
        out.append({
            "id": j.id, "title": j.title, "status": j.status,
            "progress": round(j.progress, 3),
            "message": j.message,
            "queue_position": _queue_position(jid) if j.status == "queued" else 0,
            "download": f"/api/jobs/{j.id}/download" if j.status == "done" else None,
        })
    return out


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    with _lock:
        job = JOBS.pop(job_id, None)
        if job_id in JOB_ORDER:
            JOB_ORDER.remove(job_id)
    _save_registry()
    # remove files unless it's mid-render (don't yank an active job's output)
    if job and job.status != "running":
        shutil.rmtree(os.path.join(DATA, job_id), ignore_errors=True)
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    position = _queue_position(job_id) if job.status == "queued" else 0
    return {
        "id": job.id,
        "title": job.title,
        "status": job.status,
        "progress": round(job.progress, 3),
        "message": job.message,
        "error": job.error,
        "bubble_detected": job.bubble_detected,
        "queue_position": position,
        "download": f"/api/jobs/{job.id}/download" if job.status == "done" else None,
    }


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.status != "done" or not job.output:
        raise HTTPException(404, "not ready")
    fname = os.path.basename(job.output)
    return FileResponse(job.output, media_type="video/mp4", filename=fname)


@app.get("/api/jobs/{job_id}/stream")
async def stream(job_id: str):
    """Inline (range-enabled) playback for the in-browser editor."""
    job = JOBS.get(job_id)
    if not job or job.status != "done" or not job.output:
        raise HTTPException(404, "not ready")
    return FileResponse(job.output, media_type="video/mp4")


def _process_edit(edit_id: str, src: str, spec: dict, assets: Dict[str, str]) -> None:
    job = JOBS[edit_id]
    job.status = "running"
    out_dir = os.path.join(DATA, edit_id, "out")
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(c for c in job.title if c.isalnum() or c in " -_").strip() or "edited"
    out = os.path.join(out_dir, f"{safe}.mp4")

    def progress(msg: str, frac: float) -> None:
        job.message, job.progress = msg, frac

    try:
        render_edit(src, spec, assets, out, progress=progress)
        job.output = out
        job.status = "done"
        job.message = "Done."
        job.progress = 1.0
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = f"{exc}"
        job.message = "Failed."
        traceback.print_exc()


@app.post("/api/jobs/{job_id}/edit")
async def edit_job(job_id: str, request: Request) -> dict:
    src_job = JOBS.get(job_id)
    if not src_job or src_job.status != "done" or not src_job.output:
        raise HTTPException(404, "source job not ready")

    form = await request.form()
    if "spec" not in form:
        raise HTTPException(400, "missing spec")
    spec = json.loads(form["spec"])

    edit_id = uuid.uuid4().hex[:12]
    assets_dir = os.path.join(DATA, edit_id, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    assets: Dict[str, str] = {}
    for key, val in form.multi_items():
        if key == "spec":
            continue
        if hasattr(val, "filename") and getattr(val, "filename", None):
            path = os.path.join(assets_dir, f"{key}_{val.filename}")
            with open(path, "wb") as f:
                shutil.copyfileobj(val.file, f)
            assets[key] = path

    with _lock:
        JOBS[edit_id] = Job(id=edit_id, title=f"{src_job.title} (edited)", kind="edit")
        JOB_ORDER.append(edit_id)
    _executor.submit(_process_edit, edit_id, src_job.output, spec, assets)
    return {"id": edit_id}


@app.get("/api/encoder")
async def get_encoder() -> dict:
    return {"encoder": C.ENCODER, "gpu_available": _GPU_OK}


@app.post("/api/encoder")
async def set_encoder(request: Request) -> dict:
    body = await request.json()
    mode = str(body.get("encoder", "")).lower()
    if mode not in ("cpu", "gpu"):
        raise HTTPException(400, "encoder must be 'cpu' or 'gpu'")
    if mode == "gpu" and not _GPU_OK:
        raise HTTPException(400, "GPU (NVENC) not available on this machine")
    C.ENCODER = mode
    return {"encoder": C.ENCODER}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")

_load_registry()   # restore previously finished jobs on startup
