import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..config import MEDIA_DIRS, VIDEO_EXTENSIONS

router = APIRouter()


def _scan_videos(root: str) -> list[dict]:
    results = []
    for dirpath, _, filenames in os.walk(root):
        for f in sorted(filenames):
            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root)
                results.append({
                    "name": f,
                    "path": rel,
                    "root": root,
                    "size": os.path.getsize(full),
                })
    return results


@router.get("/files")
def list_files(root: str | None = Query(None)):
    dirs = [root] if root and root in MEDIA_DIRS else MEDIA_DIRS
    files = []
    for d in dirs:
        files.extend(_scan_videos(d))
    return files


@router.get("/roots")
def list_roots():
    return MEDIA_DIRS


def _safe_resolve(path: str, root: str) -> str:
    """Join path to root and verify it stays within the root directory."""
    if root not in MEDIA_DIRS:
        raise HTTPException(status_code=400, detail="invalid root")
    full = os.path.realpath(os.path.join(root, path))
    if not full.startswith(os.path.realpath(root) + os.sep):
        raise HTTPException(status_code=403, detail="path outside media root")
    return full


@router.get("/video/{path:path}")
def serve_video(path: str, root: str = Query(...)):
    full = _safe_resolve(path, root)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(full, media_type="video/mp4")
