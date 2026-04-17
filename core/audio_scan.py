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
