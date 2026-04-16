import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from ..config import MEDIA_DIRS, QUALITY_PRESETS
from .. import cache

router = APIRouter()


def _resolve_source(path: str, root: str) -> str:
    """Join path to root, verify it stays within root, and exists."""
    if root not in MEDIA_DIRS:
        raise HTTPException(status_code=400, detail="invalid root")
    full = os.path.realpath(os.path.join(root, path))
    if not full.startswith(os.path.realpath(root) + os.sep):
        raise HTTPException(status_code=403, detail="path outside media root")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="not found")
    return full


@router.get("/stream/{path:path}")
def stream_video(path: str, root: str = Query(...), quality: str = Query("low")):
    if quality not in QUALITY_PRESETS:
        raise HTTPException(status_code=400, detail=f"invalid quality: {quality}")
    source = _resolve_source(path, root)

    status = cache.ensure_transcode(source, quality)
    if status == cache.CacheStatus.READY:
        return FileResponse(cache.cache_path(source, quality), media_type="video/mp4")
    return JSONResponse({"status": status, "quality": quality}, status_code=202)


@router.get("/audio/{path:path}")
def stream_audio(path: str, root: str = Query(...)):
    source = _resolve_source(path, root)

    status = cache.ensure_audio(source)
    if status == cache.CacheStatus.READY:
        return FileResponse(cache.audio_cache_path(source), media_type="audio/wav")
    return JSONResponse({"status": status}, status_code=202)


@router.get("/cache/status/{path:path}")
def cache_status(path: str, root: str = Query(...)):
    source = _resolve_source(path, root)
    return cache.get_all_statuses(source)
