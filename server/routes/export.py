import os
import re
import shutil
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.export import ExportRunner
from core.paths import build_export_path, build_sequence_dir
from core.ffmpeg import _RATIOS, apply_keyframes_to_jobs
from .. import ws as ws_module
from ..config import EXPORT_DIR, MEDIA_DIRS

router = APIRouter()

_jobs: dict[str, dict] = {}

_VALID_ENCODERS = {"libx264", "h264_nvenc", "h264_vaapi", "h264_qsv", "h264_amf", "h264_videotoolbox"}


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


def _next_counter(folder: str, basename: str) -> int:
    """Scan folder for existing {basename}_NNN dirs and return max + 1."""
    pattern = re.compile(rf'^{re.escape(basename)}_(\d{{3}})$')
    highest = 0
    if os.path.isdir(folder):
        for entry in os.listdir(folder):
            m = pattern.match(entry)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def _validate_input_path(path: str) -> str:
    """Verify input_path falls under a configured MEDIA_DIR."""
    real = os.path.realpath(path)
    for root in MEDIA_DIRS:
        root_real = os.path.realpath(root)
        if real == root_real or real.startswith(root_real + os.sep):
            return real
    raise HTTPException(status_code=403, detail="input_path outside media directories")


@router.post("/export")
def start_export(req: ExportRequest):
    from ..app import db

    # Validate inputs
    input_path = _validate_input_path(req.input_path)

    if req.encoder not in _VALID_ENCODERS:
        raise HTTPException(status_code=400, detail=f"invalid encoder: {req.encoder}")

    if req.portrait_ratio is not None and req.portrait_ratio not in _RATIOS:
        raise HTTPException(status_code=400, detail=f"invalid portrait_ratio: {req.portrait_ratio}")

    if req.folder_suffix and ("/" in req.folder_suffix or "\\" in req.folder_suffix or ".." in req.folder_suffix):
        raise HTTPException(status_code=400, detail="folder_suffix must not contain path separators")

    if "/" in req.name or "\\" in req.name or ".." in req.name:
        raise HTTPException(status_code=400, detail="name must not contain path separators")

    job_id = str(uuid.uuid4())[:8]
    folder = EXPORT_DIR
    if req.folder_suffix:
        folder = folder.rstrip(os.sep) + "_" + req.folder_suffix

    image_sequence = req.format in ("WebP", "WebP sequence")
    counter = _next_counter(folder, req.name)

    # Build job list: (start, output_path, portrait_ratio, crop_center)
    jobs = []
    for i in range(req.clips):
        start = req.cursor + i * req.spread
        if image_sequence:
            out = build_sequence_dir(folder, req.name, counter, sub=i if req.clips > 1 else None)
        else:
            out = build_export_path(folder, req.name, counter, sub=i if req.clips > 1 else None)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        jobs.append((start, out, req.portrait_ratio, req.crop_center))

    # Apply keyframes if provided — returns 6-tuples, strip back to 4
    if req.crop_keyframes:
        widened = apply_keyframes_to_jobs(
            jobs, req.crop_keyframes,
            req.crop_center, req.portrait_ratio,
            req.rand_portrait, req.rand_square,
        )
        jobs = [(s, o, r, c) for s, o, r, c, _rp, _rs in widened]

    completed = []

    def on_clip_done(path: str):
        completed.append(path)
        # Record in DB so markers show up
        db.add(
            filename=os.path.basename(input_path),
            start_time=req.cursor,
            output_path=path,
            label=req.label,
            category=req.category,
            short_side=req.short_side,
            portrait_ratio=req.portrait_ratio or "",
            crop_center=req.crop_center,
            fmt=req.format,
            clip_count=req.clips,
            spread=req.spread,
            profile=req.profile,
        )
        ws_module.broadcast({"type": "clip_done", "job_id": job_id, "path": path})

    def on_all_done():
        _jobs[job_id]["status"] = "done"
        _jobs[job_id].pop("runner", None)
        ws_module.broadcast({"type": "all_done", "job_id": job_id})

    def on_error(msg: str):
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = msg
        _jobs[job_id].pop("runner", None)
        ws_module.broadcast({"type": "error", "job_id": job_id, "msg": msg})

    runner = ExportRunner(
        input_path=input_path,
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
        raise HTTPException(status_code=404, detail="job not found")
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
    # Validate path is under EXPORT_DIR
    real = os.path.realpath(output_path)
    if not real.startswith(os.path.realpath(EXPORT_DIR) + os.sep):
        raise HTTPException(status_code=403, detail="path outside export directory")
    db.delete_by_output_path(real)
    if os.path.isfile(real):
        os.unlink(real)
    elif os.path.isdir(real):
        shutil.rmtree(real)
    return {"deleted": real}
