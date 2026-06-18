"""LTX-2 frame-count math. Legal F satisfy F % 8 == 1 (8x temporal + 1)."""


def is_legal_frames(f: int) -> bool:
    return f >= 9 and f % 8 == 1


def legal_frames(min_f: int = 9, max_f: int = 1000) -> list[int]:
    start = max(9, min_f + ((1 - min_f) % 8))   # first 8k+1 >= min_f
    return list(range(start, max_f + 1, 8))


def nearest_legal_frames(f: int) -> int:
    if f <= 9:
        return 9
    low = ((f - 1) // 8) * 8 + 1
    high = low + 8
    return low if (f - low) <= (high - f) else high


def duration_for_frames(frames: int, fps: float) -> float:
    return frames / fps


def frames_for_duration(duration: float, fps: float) -> int:
    return nearest_legal_frames(round(duration * fps))
