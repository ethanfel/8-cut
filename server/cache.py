import hashlib
import os
import subprocess
import threading
from enum import Enum

from core.paths import _bin, _log
from .config import CACHE_DIR, QUALITY_PRESETS


class CacheStatus(str, Enum):
    READY = "ready"
    TRANSCODING = "transcoding"
    MISSING = "missing"
    ERROR = "error"


_jobs_lock = threading.Lock()
_active_jobs: dict[str, threading.Thread] = {}


def _cache_key(source_path: str) -> str:
    """Stable hash from absolute source path."""
    return hashlib.sha256(source_path.encode()).hexdigest()[:16]


def cache_path(source_path: str, quality: str) -> str:
    key = _cache_key(source_path)
    return os.path.join(CACHE_DIR, quality, f"{key}.mp4")


def audio_cache_path(source_path: str) -> str:
    key = _cache_key(source_path)
    return os.path.join(CACHE_DIR, "audio", f"{key}.wav")


def get_status(source_path: str, quality: str) -> CacheStatus:
    cp = cache_path(source_path, quality)
    if os.path.isfile(cp):
        return CacheStatus.READY
    job_key = f"{source_path}:{quality}"
    with _jobs_lock:
        if job_key in _active_jobs and _active_jobs[job_key].is_alive():
            return CacheStatus.TRANSCODING
    return CacheStatus.MISSING


def get_audio_status(source_path: str) -> CacheStatus:
    ap = audio_cache_path(source_path)
    if os.path.isfile(ap):
        return CacheStatus.READY
    job_key = f"{source_path}:audio"
    with _jobs_lock:
        if job_key in _active_jobs and _active_jobs[job_key].is_alive():
            return CacheStatus.TRANSCODING
    return CacheStatus.MISSING


def get_all_statuses(source_path: str) -> dict:
    result = {}
    for q in QUALITY_PRESETS:
        result[q] = get_status(source_path, q)
    result["audio"] = get_audio_status(source_path)
    return result


def _transcode_worker(source_path: str, quality: str) -> None:
    preset = QUALITY_PRESETS[quality]
    out = cache_path(source_path, quality)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp.mp4"

    cmd = [_bin("ffmpeg"), "-y", "-i", source_path, "-an"]

    if preset["height"] > 0:
        cmd += [
            "-vf", f"scale=-2:{preset['height']}:flags=lanczos",
        ]

    cmd += [
        "-c:v", "libx264",
        "-preset", "fast",
        "-b:v", preset["bitrate"],
        "-movflags", "+faststart",
        tmp,
    ]

    _log(f"Transcode start: {source_path} @ {quality}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            os.rename(tmp, out)
            _log(f"Transcode done: {out}")
        else:
            _log(f"Transcode failed: {result.stderr[-300:]}")
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception as e:
        _log(f"Transcode error: {e}")
        if os.path.exists(tmp):
            os.unlink(tmp)


def _audio_extract_worker(source_path: str) -> None:
    out = audio_cache_path(source_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp.wav"

    cmd = [
        _bin("ffmpeg"), "-y",
        "-i", source_path,
        "-vn",
        "-c:a", "pcm_s16le",
        tmp,
    ]

    _log(f"Audio extract start: {source_path}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            os.rename(tmp, out)
            _log(f"Audio extract done: {out}")
        else:
            _log(f"Audio extract failed: {result.stderr[-300:]}")
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception as e:
        _log(f"Audio extract error: {e}")
        if os.path.exists(tmp):
            os.unlink(tmp)


def ensure_transcode(source_path: str, quality: str) -> CacheStatus:
    """Start transcode if not cached. Returns current status."""
    status = get_status(source_path, quality)
    if status != CacheStatus.MISSING:
        return status

    job_key = f"{source_path}:{quality}"
    with _jobs_lock:
        if job_key in _active_jobs and _active_jobs[job_key].is_alive():
            return CacheStatus.TRANSCODING
        t = threading.Thread(target=_transcode_worker, args=(source_path, quality), daemon=True)
        _active_jobs[job_key] = t
        t.start()
    return CacheStatus.TRANSCODING


def ensure_audio(source_path: str) -> CacheStatus:
    """Start audio extraction if not cached. Returns current status."""
    status = get_audio_status(source_path)
    if status != CacheStatus.MISSING:
        return status

    job_key = f"{source_path}:audio"
    with _jobs_lock:
        if job_key in _active_jobs and _active_jobs[job_key].is_alive():
            return CacheStatus.TRANSCODING
        t = threading.Thread(target=_audio_extract_worker, args=(source_path,), daemon=True)
        _active_jobs[job_key] = t
        t.start()
    return CacheStatus.TRANSCODING
