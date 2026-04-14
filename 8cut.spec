# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for 8-cut.

Usage:
    pyinstaller 8cut.spec

Platform-specific notes:
    Windows: place libmpv-2.dll, ffmpeg.exe, ffprobe.exe next to main.py
             before building, or set FFMPEG_DIR / MPV_DIR env vars.
    macOS:   place libmpv.2.dylib, ffmpeg, ffprobe next to main.py
             before building, or set FFMPEG_DIR / MPV_DIR env vars.
    Linux:   system libmpv and ffmpeg are used from PATH (not bundled).
"""

import os
import platform
import sys
from pathlib import Path

block_cipher = None
system = platform.system()

# ---------- paths ----------------------------------------------------------

base = Path(SPECPATH)
ffmpeg_dir = Path(os.environ.get("FFMPEG_DIR", base))
mpv_dir = Path(os.environ.get("MPV_DIR", base))

# ---------- data files -----------------------------------------------------

datas = []

# YOLOv8 model (optional — large, skip if missing)
yolo = base / "yolov8n.pt"
if yolo.exists():
    datas.append((str(yolo), "."))

# ---------- native binaries ------------------------------------------------

binaries = []

if system == "Windows":
    for name in ("libmpv-2.dll",):
        p = mpv_dir / name
        if p.exists():
            binaries.append((str(p), "."))
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        p = ffmpeg_dir / name
        if p.exists():
            binaries.append((str(p), "."))

elif system == "Darwin":
    for name in ("libmpv.2.dylib", "libmpv.dylib"):
        p = mpv_dir / name
        if p.exists():
            binaries.append((str(p), "."))
            break
    for name in ("ffmpeg", "ffprobe"):
        p = ffmpeg_dir / name
        if p.exists():
            binaries.append((str(p), "."))

# ---------- analysis -------------------------------------------------------

a = Analysis(
    [str(base / "main.py")],
    pathex=[str(base)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "mpv",
        "PyQt6.QtOpenGL",
        "PyQt6.QtOpenGLWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ultralytics is optional and huge — exclude from frozen build
        "ultralytics",
        "torch",
        "torchvision",
        "onnxruntime",
        "opencv-python",
        # test / dev
        "pytest",
        "hypothesis",
    ],
    noarchive=True,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, cipher=block_cipher)

# ---------- executable -----------------------------------------------------

exe_kwargs = dict(
    pyz=pyz,
    a=a,
    name="8cut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # temporary: show errors on launch
)

if system == "Darwin":
    exe_kwargs["icon"] = str(base / "assets" / "logo.png")
elif system == "Windows":
    ico = base / "assets" / "logo.ico"
    if ico.exists():
        exe_kwargs["icon"] = str(ico)

exe = EXE(**exe_kwargs)

# ---------- collect --------------------------------------------------------

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="8cut",
)

# ---------- macOS .app bundle (only on Darwin) -----------------------------

if system == "Darwin":
    app = BUNDLE(
        coll,
        name="8cut.app",
        icon=str(base / "assets" / "logo.png"),
        bundle_identifier="com.8cut.app",
        info_plist={
            "CFBundleDisplayName": "8cut",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
