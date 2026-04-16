import os
import re
import subprocess

from .paths import _bin, _log


_RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (9, 16),
    "4:5":  (4, 5),
    "1:1":  (1, 1),
}


def _portrait_crop_filter(ratio: str, crop_center: float) -> str:
    """Return an ffmpeg crop= filter expression for the given portrait ratio.

    Uses ffmpeg expression syntax so source dimensions are resolved at runtime.
    Commas inside min()/max() are escaped with \\, to prevent ffmpeg's
    filtergraph parser from treating them as filter-chain separators.
    """
    num, den = _RATIOS[ratio]
    cw = f"ih*{num}/{den}"
    x = f"max(0\\,min((iw-{cw})*{crop_center}\\,iw-{cw}))"
    return f"crop={cw}:ih:{x}:0"


def resolve_keyframe(
    keyframes: list[tuple[float, float, str | None, bool, bool]],
    t: float,
    tolerance: float = 0.05,
) -> tuple[float, float, str | None, bool, bool] | None:
    """Return the latest keyframe at or before *t*, or None."""
    result = None
    for kf in keyframes:
        if kf[0] <= t + tolerance:
            result = kf
        else:
            break
    return result


def apply_keyframes_to_jobs(
    jobs: list[tuple[float, str, str | None, float]],
    keyframes: list[tuple[float, float, str | None, bool, bool]],
    base_center: float,
    base_ratio: str | None,
    base_rand_p: bool,
    base_rand_s: bool,
) -> list[tuple[float, str, str | None, float, bool, bool]]:
    """Resolve each job's crop state from keyframes, returning widened tuples.

    Returns list of (start, path, ratio, center, rand_portrait, rand_square).
    """
    result = []
    for s, o, _r, _c in jobs:
        kf = resolve_keyframe(keyframes, s)
        if kf is not None:
            _, center, ratio, rp, rs = kf
        else:
            center, ratio, rp, rs = base_center, base_ratio, base_rand_p, base_rand_s
        result.append((s, o, ratio, center, rp, rs))
    return result


def build_ffmpeg_command(
    input_path: str, start: float, output_path: str,
    short_side: int | None = None,
    portrait_ratio: str | None = None,
    crop_center: float = 0.5,
    image_sequence: bool = False,
    encoder: str = "libx264",
) -> list[str]:
    # -ss before -i: fast input-seeking. Safe here because we always re-encode,
    # so there is no keyframe-alignment issue from pre-input seek.
    # Image sequences always use libwebp, so skip HW encoder setup.
    use_hw_vaapi = encoder == "h264_vaapi" and not image_sequence
    cmd = [_bin("ffmpeg"), "-y"]

    # VAAPI needs a device for hardware context.
    if use_hw_vaapi:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                "-vaapi_device", "/dev/dri/renderD128"]

    cmd += [
        "-threads", "0",
        "-ss", str(start),
        "-i", input_path,
        "-t", "8",
    ]

    filters: list[str] = []
    if portrait_ratio is not None:
        filters.append(_portrait_crop_filter(portrait_ratio, crop_center))
    if short_side is not None:
        # Scale so the shorter dimension equals short_side.
        filters.append(
            f"scale='if(lt(iw,ih),{short_side},-2)':'if(lt(iw,ih),-2,{short_side})':flags=lanczos"
        )

    # VAAPI: decoded frames are GPU surfaces. CPU filters need hwdownload first.
    if use_hw_vaapi:
        if filters:
            filters.insert(0, "hwdownload")
            filters.insert(1, "format=nv12")
        filters.append("format=nv12")
        filters.append("hwupload")

    if filters:
        cmd += ["-vf", ",".join(filters)]

    if image_sequence:
        cmd += [
            "-an",
            "-c:v", "libwebp",
            "-quality", "92",
            "-compression_level", "1",
            os.path.join(output_path, "frame_%04d.webp"),
        ]
    else:
        cmd += ["-c:v", encoder, "-c:a", "pcm_s16le", output_path]
    return cmd


def build_audio_extract_command(input_path: str, start: float, sequence_dir: str) -> list[str]:
    """Return an ffmpeg command that extracts audio to <sequence_dir>.wav."""
    audio_path = sequence_dir + ".wav"
    return [
        _bin("ffmpeg"), "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", "8",
        "-vn",
        "-c:a", "pcm_s16le",
        audio_path,
    ]


def detect_hw_encoders() -> list[str]:
    """Probe ffmpeg for available H.264 hardware encoders."""
    _HW_ENCODERS = ["h264_nvenc", "h264_vaapi", "h264_qsv", "h264_amf", "h264_videotoolbox"]
    try:
        result = subprocess.run(
            [_bin("ffmpeg"), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        output = result.stdout
    except Exception:
        return []
    available = []
    for enc in _HW_ENCODERS:
        if re.search(rf'\b{enc}\b', output):
            available.append(enc)
    if available:
        _log(f"HW encoders detected: {', '.join(available)}")
    else:
        _log("No HW encoders detected — GPU export unavailable")
    return available
