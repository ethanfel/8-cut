import os

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

from ..config import MEDIA_DIRS, QUALITY_PRESETS
from .. import cache

router = APIRouter()


def _resolve_source(path: str, root: str) -> str | None:
    if root not in MEDIA_DIRS:
        return None
    full = os.path.join(root, path)
    return full if os.path.isfile(full) else None


@router.get("/stream/{path:path}")
def stream_video(path: str, root: str = Query(...), quality: str = Query("low")):
    if quality not in QUALITY_PRESETS:
        return JSONResponse({"error": f"invalid quality: {quality}"}, status_code=400)
    source = _resolve_source(path, root)
    if source is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    status = cache.ensure_transcode(source, quality)
    if status == cache.CacheStatus.READY:
        return FileResponse(cache.cache_path(source, quality), media_type="video/mp4")
    return JSONResponse({"status": status, "quality": quality}, status_code=202)


@router.get("/audio/{path:path}")
def stream_audio(path: str, root: str = Query(...)):
    source = _resolve_source(path, root)
    if source is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    status = cache.ensure_audio(source)
    if status == cache.CacheStatus.READY:
        return FileResponse(cache.audio_cache_path(source), media_type="audio/wav")
    return JSONResponse({"status": status}, status_code=202)


@router.get("/cache/status/{path:path}")
def cache_status(path: str, root: str = Query(...)):
    source = _resolve_source(path, root)
    if source is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return cache.get_all_statuses(source)
