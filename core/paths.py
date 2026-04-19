import os
import sys
from datetime import datetime
from pathlib import Path


def _frozen_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _bin(name: str) -> str:
    """Resolve a binary name (e.g. 'ffmpeg') to its full path in frozen builds."""
    p = _frozen_path() / name
    if p.exists():
        return str(p)
    return name  # fall back to PATH


def _log(*args) -> None:
    """Print a timestamped log line to stderr."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[8-cut {ts}]", *args, file=sys.stderr)


def build_export_path(folder: str, basename: str, counter: int, sub: int | None = None) -> str:
    """Build clip output path.  *folder* should be the vid folder (e.g. .../mp4/vid_001)."""
    name = f"{basename}_{counter:03d}"
    if sub is not None:
        name = f"{name}_{sub}"
    return os.path.join(folder, name + ".mp4")


def build_sequence_dir(folder: str, basename: str, counter: int, sub: int | None = None) -> str:
    """Build WebP sequence output dir.  *folder* should be the vid folder."""
    name = f"{basename}_{counter:03d}"
    if sub is not None:
        name = f"{name}_{sub}"
    return os.path.join(folder, name)


def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    # Floor-truncate to 1 dp (not round) — prevents "X:60.0" rollover when
    # seconds is e.g. 59.95.
    s = int(seconds % 60 * 10) / 10
    return f"{m}:{s:04.1f}"
