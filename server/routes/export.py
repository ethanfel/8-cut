import os
import shutil
import uuid

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel

from core.export import ExportRunner
from core.paths import build_export_path, build_sequence_dir
from core.ffmpeg import apply_keyframes_to_jobs
from .. import ws as ws_module
from ..config import EXPORT_DIR

router = APIRouter()

_jobs: dict[str, dict] = {}


class ExportRequest(BaseModel):
    input_path: str
    cursor: float
    name: str
    clips: int = 3
    spread: float = 3.0
    short_side: int | None = None
    portrait_ratio: str | None = None
    crop_center: float = 0.5
    format: str = "MP4"
    label: str = ""
    category: str = ""
    profile: str = "default"
    folder_suffix: str = ""
    crop_keyframes: list | None = None
    rand_portrait: bool = False
    rand_square: bool = False
    encoder: str = "libx264"


@router.post("/export")
def start_export(req: ExportRequest):
    job_id = str(uuid.uuid4())[:8]
    folder = EXPORT_DIR
    if req.folder_suffix:
        folder = folder + req.folder_suffix

    image_sequence = req.format == "WebP"

    # Build job list: (start, output_path, portrait_ratio, crop_center)
    jobs = []
    for i in range(req.clips):
        start = req.cursor + i * req.spread
        counter = 1  # server uses simple incrementing
        if image_sequence:
            out = build_sequence_dir(folder, req.name, counter, sub=i if req.clips > 1 else None)
        else:
            out = build_export_path(folder, req.name, counter, sub=i if req.clips > 1 else None)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        jobs.append((start, out, req.portrait_ratio, req.crop_center))

    # Apply keyframes if provided
    if req.crop_keyframes:
        jobs = apply_keyframes_to_jobs(
            jobs, req.crop_keyframes,
            req.crop_center, req.portrait_ratio,
            req.rand_portrait, req.rand_square,
        )

    completed = []

    def on_clip_done(path: str):
        completed.append(path)
        ws_module.broadcast({"type": "clip_done", "job_id": job_id, "path": path})

    def on_all_done():
        _jobs[job_id]["status"] = "done"
        ws_module.broadcast({"type": "all_done", "job_id": job_id})

    def on_error(msg: str):
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = msg
        ws_module.broadcast({"type": "error", "job_id": job_id, "msg": msg})

    runner = ExportRunner(
        input_path=req.input_path,
        jobs=jobs,
        short_side=req.short_side,
        image_sequence=image_sequence,
        encoder=req.encoder,
        on_clip_done=on_clip_done,
        on_all_done=on_all_done,
        on_error=on_error,
    )

    _jobs[job_id] = {
        "status": "running",
        "total": len(jobs),
        "completed": completed,
        "runner": runner,
    }
    runner.start()

    return {"job_id": job_id}


@router.get("/export/{job_id}")
def get_export_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return {"error": "job not found"}
    return {
        "status": job["status"],
        "total": job["total"],
        "completed": len(job["completed"]),
        "outputs": list(job["completed"]),
        "error": job.get("error"),
    }


@router.delete("/export/{output_path:path}")
def delete_export(output_path: str):
    from ..app import db
    db.delete_by_output_path(output_path)
    if os.path.isfile(output_path):
        os.unlink(output_path)
    elif os.path.isdir(output_path):
        shutil.rmtree(output_path)
    return {"deleted": output_path}


@router.websocket("/ws/export")
async def export_ws(websocket: WebSocket):
    await ws_module.connect(websocket)
