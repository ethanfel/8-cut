import os
from pathlib import Path


MEDIA_DIRS: list[str] = [
    d.strip() for d in os.environ.get("MEDIA_DIRS", str(Path.home())).split(",") if d.strip()
]
EXPORT_DIR: str = os.environ.get("EXPORT_DIR", str(Path.home() / "8cut-exports"))
DB_PATH: str = os.environ.get("DB_PATH", str(Path.home() / ".8cut.db"))
CACHE_DIR: str = os.environ.get("CACHE_DIR", str(Path.home() / ".8cut-cache"))
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8000"))

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".flv", ".wmv"}

QUALITY_PRESETS = {
    "potato": {"height": 480, "bitrate": "500k"},
    "low":    {"height": 720, "bitrate": "2M"},
    "medium": {"height": 1080, "bitrate": "5M"},
    "high":   {"height": 0, "bitrate": "10M"},  # 0 = original resolution
}
