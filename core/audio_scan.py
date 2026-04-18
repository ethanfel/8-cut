"""Audio scanning — embedding-based classifier for audio event detection."""

import hashlib
import os
import numpy as np
import librosa

from .paths import _log

_SR = 16000           # lower sr = faster
_WINDOW = 8.0         # seconds
_MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
_W2V_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".8cut_cache", "w2v")

# ---------------------------------------------------------------------------
# Embedding extraction (lazy-loaded)
# ---------------------------------------------------------------------------

_w2v_model = None
_w2v_device = None
_w2v_model_name = None

# Supported embedding models — name → embed_dim
_EMBED_MODELS = {
    "WAV2VEC2_BASE":       768,
    "WAV2VEC2_LARGE":      1024,
    "WAV2VEC2_LARGE_LV60K":1024,
    "HUBERT_BASE":         768,
    "HUBERT_LARGE":        1024,
    "HUBERT_XLARGE":       1280,
    "BEATS":               768,
}
_DEFAULT_EMBED_MODEL = "WAV2VEC2_BASE"

_BEATS_CHECKPOINT = os.path.join(
    os.path.expanduser("~"), ".cache", "huggingface", "hub",
    "models--lpepino--beats_ckpts", "snapshots",
    "5b53b0404df452a3a607d7e67687227730e5bad1", "BEATs_iter3_plus_AS2M.pt",
)


def _get_w2v_model(model_name: str | None = None):
    """Lazy-load an embedding model. Reloads if model_name differs from cached."""
    global _w2v_model, _w2v_device, _w2v_model_name
    if model_name is None:
        model_name = _DEFAULT_EMBED_MODEL
    if _w2v_model is None or _w2v_model_name != model_name:
        import torch
        _w2v_device = "cuda" if torch.cuda.is_available() else "cpu"

        if model_name == "BEATS":
            from .beats_model import BEATs, BEATsConfig
            checkpoint = torch.load(_BEATS_CHECKPOINT, map_location=_w2v_device,
                                    weights_only=False)
            cfg = BEATsConfig(checkpoint['cfg'])
            _w2v_model = BEATs(cfg)
            _w2v_model.load_state_dict(checkpoint['model'])
            _w2v_model.to(_w2v_device)
        else:
            import torchaudio
            bundle = getattr(torchaudio.pipelines, model_name)
            _w2v_model = bundle.get_model().to(_w2v_device)

        _w2v_model.eval()
        _w2v_model_name = model_name
        _log(f"audio_scan: {model_name} loaded on {_w2v_device}")
    return _w2v_model, _w2v_device


def _embed_dim(model_name: str | None = None) -> int:
    """Return embedding dimension for a model name."""
    if model_name is None:
        model_name = _DEFAULT_EMBED_MODEL
    return _EMBED_MODELS.get(model_name, 768)


def _w2v_cache_path(video_path: str, hop: float, window: float,
                    model_name: str | None = None) -> str:
    """Return cache file path for a video's embeddings (includes model name)."""
    if model_name is None:
        model_name = _DEFAULT_EMBED_MODEL
    abspath = os.path.abspath(video_path)
    mtime = os.path.getmtime(abspath)
    key = f"{abspath}|{mtime}|{hop}|{window}|{model_name}"
    h = hashlib.sha256(key.encode()).hexdigest()[:16]
    return os.path.join(_W2V_CACHE_DIR, f"{h}.npz")


def _extract_w2v_windows(y: np.ndarray, sr: int = _SR,
                         hop: float = 1.0, window: float = _WINDOW,
                         video_path: str | None = None,
                         cancel_flag: object = None,
                         model_name: str | None = None,
                         ) -> tuple[np.ndarray, np.ndarray]:
    """Extract embeddings for all sliding windows using a torchaudio model.

    If video_path is given, results are cached to disk for fast re-scans.
    Returns (timestamps, embeddings) where embeddings is (N, D).
    """
    edim = _embed_dim(model_name)

    # Try loading from cache
    cache_file = None
    if video_path:
        try:
            cache_file = _w2v_cache_path(video_path, hop, window, model_name)
            if os.path.exists(cache_file):
                data = np.load(cache_file)
                _log(f"audio_scan: cache hit ({cache_file})")
                return data["timestamps"], data["embeddings"]
        except Exception as e:
            _log(f"audio_scan: cache read failed: {e}")

    win_samples = int(window * sr)
    hop_samples = int(hop * sr)
    n_windows = max(0, (len(y) - win_samples) // hop_samples + 1)

    if n_windows == 0:
        return np.array([]), np.empty((0, edim))

    import torch
    model, device = _get_w2v_model(model_name)
    is_beats = (model_name or _DEFAULT_EMBED_MODEL) == "BEATS"
    batch_size = 16
    timestamps = np.arange(n_windows) * hop
    embeddings = []

    for batch_start in range(0, n_windows, batch_size):
        if cancel_flag and getattr(cancel_flag, '_cancel', False):
            return np.array([]), np.empty((0, edim))
        batch_end = min(batch_start + batch_size, n_windows)
        chunks = []
        for i in range(batch_start, batch_end):
            start = i * hop_samples
            chunks.append(y[start:start + win_samples])
        with torch.no_grad():
            waveforms = torch.from_numpy(np.stack(chunks)).float().to(device)
            if is_beats:
                padding_mask = torch.zeros_like(waveforms, dtype=torch.bool)
                features, _ = model.extract_features(waveforms, padding_mask=padding_mask)
            else:
                features, _ = model(waveforms)
            batch_emb = features.mean(dim=1).cpu().numpy()
        embeddings.append(batch_emb)

    result_ts = timestamps
    result_emb = np.vstack(embeddings)

    # Save to cache
    if cache_file:
        try:
            os.makedirs(_W2V_CACHE_DIR, exist_ok=True)
            np.savez(cache_file, timestamps=result_ts, embeddings=result_emb)
            _log(f"audio_scan: w2v cache saved ({cache_file})")
        except Exception as e:
            _log(f"audio_scan: cache write failed: {e}")

    return result_ts, result_emb


def _extract_w2v_targeted(y: np.ndarray, sr: int, gt_intense: list[float],
                          gt_soft: list[float], tolerance: float = 12.0,
                          neg_margin: float = 120.0,
                          model_name: str | None = None,
                          ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Extract embeddings only near positives and distant negatives.

    Returns (timestamps, embeddings, labels) where labels: 1=pos, -1=neg, 0=ambig.
    """
    edim = _embed_dim(model_name)
    duration = len(y) / sr
    win_samples = int(_WINDOW * sr)
    all_gt = list(gt_intense) + list(gt_soft)

    # Positive windows: every second near intense markers
    pos_times = set()
    for gt in gt_intense:
        for offset in range(-int(tolerance), int(tolerance) + 1):
            t = gt + offset
            if 0 <= t <= duration - _WINDOW:
                pos_times.add(int(t))

    # Negative windows: every 4s, far from any marker
    neg_times = set()
    for t in range(0, int(duration - _WINDOW), 4):
        if min((abs(t - g) for g in all_gt), default=9999) > neg_margin:
            neg_times.add(t)

    all_times = sorted(pos_times | neg_times)
    # Filter out windows that go past the end
    valid_times = [t for t in all_times if int(t * sr) + win_samples <= len(y)]

    if not valid_times:
        return np.array([]), np.zeros((0, edim)), np.array([], dtype=int)

    import torch
    model, device = _get_w2v_model(model_name)
    batch_size = 16
    timestamps_list: list[float] = []
    embeddings_list: list[np.ndarray] = []

    is_beats = (model_name or _DEFAULT_EMBED_MODEL) == "BEATS"

    for batch_start in range(0, len(valid_times), batch_size):
        batch_end = min(batch_start + batch_size, len(valid_times))
        chunks = []
        for t in valid_times[batch_start:batch_end]:
            start = int(t * sr)
            chunks.append(y[start:start + win_samples])
            timestamps_list.append(float(t))
        with torch.no_grad():
            waveforms = torch.from_numpy(np.stack(chunks)).float().to(device)
            if is_beats:
                padding_mask = torch.zeros_like(waveforms, dtype=torch.bool)
                features, _ = model.extract_features(waveforms, padding_mask=padding_mask)
            else:
                features, _ = model(waveforms)
            batch_emb = features.mean(dim=1).cpu().numpy()
        embeddings_list.append(batch_emb)

    timestamps = np.array(timestamps_list)
    embeddings = np.vstack(embeddings_list)

    labels = np.zeros(len(timestamps), dtype=int)
    for i, t in enumerate(timestamps):
        di = min((abs(t - g) for g in gt_intense), default=9999)
        da = min((abs(t - g) for g in all_gt), default=9999)
        if di < tolerance:
            labels[i] = 1
        elif da > neg_margin:
            labels[i] = -1
    return timestamps, embeddings, labels


# ---------------------------------------------------------------------------
# Classifier mode — train / save / load / scan
# ---------------------------------------------------------------------------

def train_classifier(video_infos: list[tuple[str, list[float], list[float]]],
                     model_path: str | None = None,
                     tolerance: float = 12.0,
                     neg_margin: float = 120.0,
                     embed_model: str | None = None,
                     cancel_flag: object = None) -> dict:
    """Train a classifier from labeled videos.

    Args:
        video_infos: list of (video_path, intense_times, soft_times)
        model_path: if given, save model to this path
        tolerance/neg_margin: labeling parameters
        embed_model: embedding model name (e.g. "HUBERT_BASE", "BEATS"), defaults to WAV2VEC2_BASE
        cancel_flag: object with _cancel attribute; if set, training aborts early

    Returns:
        dict with 'classifier', 'embed_model', and metadata, or None on failure.
    """
    from sklearn.ensemble import GradientBoostingClassifier

    all_X, all_y = [], []

    for vi, (vpath, gt_intense, gt_soft) in enumerate(video_infos):
        if cancel_flag and getattr(cancel_flag, '_cancel', False):
            _log("audio_scan: training cancelled")
            return None
        _log(f"audio_scan: training [{vi+1}/{len(video_infos)}] {os.path.basename(vpath)}")
        y, _ = librosa.load(vpath, sr=_SR, mono=True)

        timestamps, embeddings, labels = _extract_w2v_targeted(
            y, _SR, gt_intense, gt_soft, tolerance, neg_margin,
            model_name=embed_model,
        )
        if len(timestamps) == 0:
            continue
        # Per-video z-score normalize
        vid_mean = embeddings.mean(axis=0)
        vid_std = np.maximum(embeddings.std(axis=0), 1e-6)
        normed = (embeddings - vid_mean) / vid_std
        for i in range(len(labels)):
            if labels[i] == 1:
                all_X.append(normed[i])
                all_y.append(1)
            elif labels[i] == -1:
                all_X.append(normed[i])
                all_y.append(0)

    if not all_X:
        _log("audio_scan: no training samples collected")
        return None

    X = np.stack(all_X)
    y_arr = np.array(all_y)
    n_pos = (y_arr == 1).sum()
    n_neg = (y_arr == 0).sum()
    _log(f"audio_scan: training set — {n_pos} positive, {n_neg} negative")

    if n_pos == 0 or n_neg == 0:
        _log(f"audio_scan: need both classes — {n_pos} pos, {n_neg} neg")
        return None

    # Subsample negatives for balance
    rng = np.random.RandomState(42)
    pos_idx = np.where(y_arr == 1)[0]
    neg_idx = np.where(y_arr == 0)[0]
    n_neg_sample = min(len(neg_idx), len(pos_idx) * 3)
    neg_sample = rng.choice(neg_idx, n_neg_sample, replace=False)
    train_idx = np.concatenate([pos_idx, neg_sample])
    rng.shuffle(train_idx)

    clf = GradientBoostingClassifier(
        n_estimators=200, max_depth=5, learning_rate=0.1, random_state=42,
    )
    clf.fit(X[train_idx], y_arr[train_idx])
    _log("audio_scan: classifier trained")

    model = {"classifier": clf, "n_features": X.shape[1],
             "embed_model": embed_model or _DEFAULT_EMBED_MODEL}

    if model_path:
        import joblib
        parent = os.path.dirname(model_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        joblib.dump(model, model_path)
        _log(f"audio_scan: model saved to {model_path}")

    return model


def load_classifier(model_path: str) -> dict | None:
    """Load a saved classifier model."""
    if not os.path.exists(model_path):
        return None
    import joblib
    return joblib.load(model_path)


def default_model_path(profile_name: str = "default") -> str:
    """Return the default path for a profile's classifier model."""
    return os.path.join(_MODEL_DIR, f"{profile_name}.joblib")


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan_video(
    video_path: str,
    model: dict = None,
    threshold: float = 0.30,
    hop: float = 1.0,
    window: float = _WINDOW,
    cancel_flag: object = None,
) -> list[tuple[float, float, float]]:
    """Scan a video for matching audio regions using a trained classifier.

    Returns list of (start_time, end_time, score) above threshold.
    """
    if model is None:
        _log("audio_scan: no model provided")
        return []

    _log(f"audio_scan: loading {video_path}")
    y, sr = librosa.load(video_path, sr=_SR, mono=True)
    duration = len(y) / sr
    _log(f"audio_scan: {duration:.1f}s loaded, extracting features...")

    if cancel_flag and getattr(cancel_flag, '_cancel', False):
        return []

    clf = model["classifier"]
    embed_model = model.get("embed_model")

    _log(f"audio_scan: extracting embeddings ({embed_model or 'default'})...")
    timestamps, window_vectors = _extract_w2v_windows(
        y, sr, hop=hop, window=window, video_path=video_path,
        cancel_flag=cancel_flag, model_name=embed_model,
    )
    if len(timestamps) == 0:
        _log("audio_scan: video shorter than window")
        return []

    # Per-video z-score normalize
    vid_mean = window_vectors.mean(axis=0)
    vid_std = np.maximum(window_vectors.std(axis=0), 1e-6)
    normed = (window_vectors - vid_mean) / vid_std

    _log(f"audio_scan: classifying {len(normed)} windows...")

    if cancel_flag and getattr(cancel_flag, '_cancel', False):
        return []

    probs = clf.predict_proba(normed)[:, 1]
    mask = probs >= threshold
    results = [
        (timestamps[i], timestamps[i] + window, float(probs[i]))
        for i in np.nonzero(mask)[0]
    ]
    _log(f"audio_scan: {len(results)} regions above threshold {threshold}")
    return results
