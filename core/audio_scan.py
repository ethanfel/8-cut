"""Audio similarity scanning — MFCC + spectral contrast profile matching."""

import numpy as np
import librosa

from .paths import _log

_N_MFCC = 13          # coefficients 0-12; we drop C0 → 12 usable
_SR = 16000           # lower sr = faster, no quality loss for style matching
_HOP_LENGTH = 1024    # STFT hop (~64ms frames at 16kHz)
_N_FFT = 2048         # STFT window
_WINDOW = 8.0         # seconds
_N_FEATURES = 62      # (12 mfcc + 12 delta + 7 sc) * 2 (mean + std)


def _extract_features_from_signal(y: np.ndarray, sr: int = _SR) -> np.ndarray:
    """Compute feature matrix (31 x T) from a raw audio signal.

    Features per frame: 12 MFCCs (skip C0) + 12 delta MFCCs + 7 spectral contrast.
    """
    S = np.abs(librosa.stft(y, n_fft=_N_FFT, hop_length=_HOP_LENGTH)) ** 2
    mel_S = librosa.feature.melspectrogram(S=S, sr=sr, hop_length=_HOP_LENGTH)
    mfcc = librosa.feature.mfcc(S=librosa.power_to_db(mel_S), sr=sr, n_mfcc=_N_MFCC)
    mfcc = mfcc[1:]  # drop C0 (energy) — dominates cosine sim, kills discrimination
    delta = librosa.feature.delta(mfcc)
    sc = librosa.feature.spectral_contrast(S=S, sr=sr, hop_length=_HOP_LENGTH)
    return np.vstack([mfcc, delta, sc])  # (31, T)


def _aggregate(feature_matrix: np.ndarray) -> np.ndarray:
    """Collapse a (31, T) feature matrix into a (62,) vector via mean + std."""
    return np.concatenate([
        feature_matrix.mean(axis=1),
        feature_matrix.std(axis=1),
    ])


def _extract_features(path: str, sr: int = _SR) -> np.ndarray:
    """Load audio from a file and return a 62-dim feature vector."""
    y, _ = librosa.load(path, sr=sr, mono=True)
    feat = _extract_features_from_signal(y, sr)
    return _aggregate(feat)


def build_profile(clip_paths: list[str]) -> dict | None:
    """Extract features from reference clips.

    Returns dict with:
      - mean_vector: averaged feature vector across all clips (62,)
      - clip_vectors: list of individual feature vectors
    Returns None if no clips could be loaded.
    """
    vectors = []
    for p in clip_paths:
        try:
            vec = _extract_features(p)
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


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Euclidean-distance-based similarity in (0, 1].

    1/(1+dist): identical → 1.0, very different → near 0.
    """
    return float(1.0 / (1.0 + np.linalg.norm(a - b)))


def scan_video(
    video_path: str,
    profile: dict,
    mode: str = "average",
    threshold: float = 0.05,
    hop: float = 1.0,
    window: float = _WINDOW,
    cancel_flag: object = None,
) -> list[tuple[float, float, float]]:
    """Slide a window across the video audio and score against the profile.

    Pre-computes STFT once for the whole file, then uses vectorized
    cumulative-sum sliding window for speed.

    Args:
        video_path: path to video/audio file
        profile: dict from build_profile()
        mode: "average" (compare to mean) or "nearest" (max over all clips)
        threshold: minimum similarity to include (0-1, default 0.05)
        hop: step size in seconds
        window: window size in seconds (default 8s)
        cancel_flag: object with _cancel bool attribute; checked periodically

    Returns:
        list of (start_time, end_time, score) for regions above threshold
    """
    _log(f"audio_scan: loading {video_path}")
    y, sr = librosa.load(video_path, sr=_SR, mono=True)
    duration = len(y) / sr
    _log(f"audio_scan: {duration:.1f}s loaded, extracting features...")

    if cancel_flag and getattr(cancel_flag, '_cancel', False):
        return []

    # Compute features for the entire file at once (one STFT)
    feat = _extract_features_from_signal(y, sr)  # (31, T)
    n_feats, T = feat.shape
    fps = sr / _HOP_LENGTH  # frames per second
    win_frames = int(window * fps)
    hop_frames = int(hop * fps)

    if win_frames > T:
        _log("audio_scan: video shorter than window")
        return []

    _log(f"audio_scan: scanning {T} frames, win={win_frames}, hop={hop_frames}")

    # Vectorized sliding window via cumulative sums
    cumsum = np.zeros((n_feats, T + 1))
    cumsum[:, 1:] = np.cumsum(feat, axis=1)
    cumsq = np.zeros((n_feats, T + 1))
    cumsq[:, 1:] = np.cumsum(feat ** 2, axis=1)

    starts = np.arange(0, T - win_frames + 1, hop_frames)
    ends = starts + win_frames

    sums = cumsum[:, ends] - cumsum[:, starts]        # (31, n_windows)
    sq_sums = cumsq[:, ends] - cumsq[:, starts]
    means = sums / win_frames
    stds = np.sqrt(np.maximum(sq_sums / win_frames - means ** 2, 0) + 1e-10)

    window_vectors = np.vstack([means, stds]).T  # (n_windows, 62)

    if cancel_flag and getattr(cancel_flag, '_cancel', False):
        return []

    # Score all windows
    if mode == "nearest":
        # Compare each window to every clip vector, take max
        clip_vecs = np.stack(profile["clip_vectors"])  # (n_clips, 62)
        results = []
        # Process in batches to check cancel_flag periodically
        batch = 500
        for i in range(0, len(window_vectors), batch):
            if cancel_flag and getattr(cancel_flag, '_cancel', False):
                _log("audio_scan: cancelled")
                return results
            chunk = window_vectors[i:i + batch]
            # cdist: (batch, n_clips) distances
            dists = np.linalg.norm(chunk[:, None, :] - clip_vecs[None, :, :], axis=2)
            scores = 1.0 / (1.0 + dists.min(axis=1))  # min dist = max similarity
            for j, score in enumerate(scores):
                if score >= threshold:
                    idx = i + j
                    start_t = starts[idx] / fps
                    results.append((start_t, start_t + window, float(score)))
    else:
        # Average mode: compare to mean vector
        ref = profile["mean_vector"]
        dists = np.linalg.norm(window_vectors - ref, axis=1)
        scores = 1.0 / (1.0 + dists)
        mask = scores >= threshold
        results = [
            (starts[i] / fps, starts[i] / fps + window, float(scores[i]))
            for i in np.nonzero(mask)[0]
        ]

    _log(f"audio_scan: {len(results)} regions above threshold {threshold}")
    return results
