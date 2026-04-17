"""Audio similarity scanning — MFCC-based profile matching."""

import numpy as np
import librosa

from .paths import _log

_N_MFCC = 20
_SR = 22050


def _extract_mfcc(path: str, sr: int = _SR) -> np.ndarray:
    """Load audio from a file and return a mean MFCC vector (20-dim)."""
    y, _ = librosa.load(path, sr=sr, mono=True)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=_N_MFCC)
    return mfcc.mean(axis=1)  # average over time → (20,)


def build_profile(clip_paths: list[str]) -> dict | None:
    """Extract MFCCs from reference clips.

    Returns dict with:
      - mean_vector: averaged MFCC across all clips (20,)
      - clip_vectors: list of individual MFCC vectors
    Returns None if no clips could be loaded.
    """
    vectors = []
    for p in clip_paths:
        try:
            vec = _extract_mfcc(p)
            vectors.append(vec)
        except Exception as e:
            _log(f"audio_scan: skip {p}: {e}")
    if not vectors:
        return None
    arr = np.stack(vectors)
    return {
        "mean_vector": arr.mean(axis=0),
        "clip_vectors": vectors,
    }


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors.

    Returns value in [-1, 1]. Negative means anti-correlated (very
    dissimilar). For threshold filtering this is fine — negative scores
    never exceed the threshold. Scores near 0 may be uncorrelated or
    weakly anti-correlated.
    """
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def scan_video(
    video_path: str,
    profile: dict,
    mode: str = "average",
    threshold: float = 0.7,
    hop: float = 1.0,
    window: float = 8.0,
    cancel_flag: object = None,
) -> list[tuple[float, float, float]]:
    """Slide a window across the video audio and score against the profile.

    Args:
        video_path: path to video/audio file
        profile: dict from build_profile()
        mode: "average" (compare to mean) or "nearest" (max over all clips)
        threshold: minimum cosine similarity to include
        hop: step size in seconds
        window: window size in seconds (default 8s)
        cancel_flag: object with _cancel bool attribute; checked each iteration

    Returns:
        list of (start_time, end_time, score) for regions above threshold
    """
    _log(f"audio_scan: loading {video_path}")
    y, sr = librosa.load(video_path, sr=_SR, mono=True)
    duration = len(y) / sr
    _log(f"audio_scan: {duration:.1f}s loaded, scanning with hop={hop}s")

    win_samples = int(window * sr)
    hop_samples = int(hop * sr)

    results = []
    pos = 0
    while pos + win_samples <= len(y):
        if cancel_flag and getattr(cancel_flag, '_cancel', False):
            _log("audio_scan: cancelled")
            return results

        chunk = y[pos : pos + win_samples]
        mfcc = librosa.feature.mfcc(y=chunk, sr=sr, n_mfcc=_N_MFCC)
        vec = mfcc.mean(axis=1)

        if mode == "nearest":
            score = max(
                _cosine_similarity(vec, cv) for cv in profile["clip_vectors"]
            )
        else:  # average
            score = _cosine_similarity(vec, profile["mean_vector"])

        if score >= threshold:
            start_t = pos / sr
            results.append((start_t, start_t + window, score))

        pos += hop_samples

    _log(f"audio_scan: {len(results)} regions above threshold {threshold}")
    return results
