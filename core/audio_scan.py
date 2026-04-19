"""Audio scanning — embedding-based classifier for audio event detection."""

import hashlib
import os
import subprocess
import numpy as np

from .paths import _bin, _log

_SR = 16000           # lower sr = faster


def _load_audio_ffmpeg(path: str, sr: int = _SR) -> np.ndarray:
    """Load audio from any file as mono float32 numpy array using ffmpeg directly."""
    cmd = [
        _bin("ffmpeg"), "-i", path,
        "-vn",                    # skip video
        "-ac", "1",               # mono
        "-ar", str(sr),           # resample
        "-f", "f32le",            # raw 32-bit float little-endian
        "-loglevel", "error",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timed out (300s) on {os.path.basename(path)}")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode().strip()}")
    return np.frombuffer(proc.stdout, dtype=np.float32)
_WINDOW = 8.0         # seconds
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODEL_DIR = os.path.join(_PROJECT_DIR, "models")
_W2V_CACHE_DIR = os.path.join(_PROJECT_DIR, "cache", "w2v")
_DL_CACHE_DIR = os.path.join(_PROJECT_DIR, "cache", "downloads")

# Redirect torch hub and huggingface downloads into the project
os.environ.setdefault("TORCH_HOME", _DL_CACHE_DIR)
os.environ.setdefault("HF_HOME", os.path.join(_DL_CACHE_DIR, "huggingface"))

# ---------------------------------------------------------------------------
# Embedding extraction (lazy-loaded)
# ---------------------------------------------------------------------------

_w2v_model = None
_w2v_device = None
_w2v_model_name = None
_ast_feature_extractor = None

# Supported embedding models — name → embed_dim
_EMBED_MODELS = {
    "WAV2VEC2_BASE":       768,
    "WAV2VEC2_LARGE":      1024,
    "WAV2VEC2_LARGE_LV60K":1024,
    "HUBERT_BASE":         768,
    "HUBERT_LARGE":        1024,
    "HUBERT_XLARGE":       1280,
    "BEATS":               768,
    # Multi-layer variants (4 quartile layers concatenated)
    "WAV2VEC2_BASE_ML":   3072,   # 768 * 4
    "HUBERT_BASE_ML":     3072,   # 768 * 4
    "HUBERT_LARGE_ML":    4096,   # 1024 * 4
    "HUBERT_XLARGE_ML":   5120,   # 1280 * 4
    # Transformers-based models
    "AST":                 768,
    "AST_ML":             3072,   # 768 * 4
    "EAT":                 768,
}
_DEFAULT_EMBED_MODEL = "WAV2VEC2_BASE"

_BEATS_CHECKPOINT = os.path.join(
    _DL_CACHE_DIR, "huggingface", "hub",
    "models--lpepino--beats_ckpts", "snapshots",
    "5b53b0404df452a3a607d7e67687227730e5bad1", "BEATs_iter3_plus_AS2M.pt",
)


def _get_w2v_model(model_name: str | None = None):
    """Lazy-load an embedding model. Reloads if model_name differs from cached."""
    global _w2v_model, _w2v_device, _w2v_model_name
    if model_name is None:
        model_name = _DEFAULT_EMBED_MODEL
    # Multi-layer variants use the same base model weights
    ml = _ml_config(model_name)
    load_name = ml[0] if ml else model_name
    if _w2v_model is None or _w2v_model_name != load_name:
        import torch
        _w2v_device = "cuda" if torch.cuda.is_available() else "cpu"

        if load_name == "BEATS":
            from .beats_model import BEATs, BEATsConfig
            checkpoint = torch.load(_BEATS_CHECKPOINT, map_location=_w2v_device,
                                    weights_only=False)
            cfg = BEATsConfig(checkpoint['cfg'])
            _w2v_model = BEATs(cfg)
            _w2v_model.load_state_dict(checkpoint['model'])
            _w2v_model.to(_w2v_device)
        elif load_name == "AST":
            from transformers import ASTModel, ASTFeatureExtractor
            _w2v_model = ASTModel.from_pretrained(
                "MIT/ast-finetuned-audioset-10-10-0.4593"
            ).to(_w2v_device)
            global _ast_feature_extractor
            _ast_feature_extractor = ASTFeatureExtractor.from_pretrained(
                "MIT/ast-finetuned-audioset-10-10-0.4593"
            )
        elif load_name == "EAT":
            from transformers import AutoModel
            _w2v_model = AutoModel.from_pretrained(
                "worstchan/EAT-base_epoch30_finetune_AS2M",
                trust_remote_code=True,
            ).to(_w2v_device)
        else:
            import torchaudio
            bundle = getattr(torchaudio.pipelines, load_name)
            _w2v_model = bundle.get_model().to(_w2v_device)

        _w2v_model.eval()
        _w2v_model_name = load_name
        _log(f"audio_scan: {load_name} loaded on {_w2v_device}")
    return _w2v_model, _w2v_device


def _eat_preprocess(chunks: list[np.ndarray], sr: int, device: str):
    """Convert raw audio chunks to EAT mel spectrogram input.

    Returns tensor of shape [B, 1, T, 128].
    8s audio at 10ms frame shift produces ~798 frames, zero-padded to 1024.
    """
    import torch
    import torchaudio.compliance.kaldi as kaldi

    TARGET_LEN = 1024
    MEAN, STD = -4.268, 4.569

    mels = []
    for chunk in chunks:
        wav = torch.from_numpy(chunk).unsqueeze(0).float()
        fbank = kaldi.fbank(
            wav, htk_compat=True, sample_frequency=sr, use_energy=False,
            window_type='hanning', num_mel_bins=128, dither=0.0, frame_shift=10,
        )
        # Pad or truncate to TARGET_LEN
        if fbank.shape[0] < TARGET_LEN:
            fbank = torch.nn.functional.pad(fbank, (0, 0, 0, TARGET_LEN - fbank.shape[0]))
        else:
            fbank = fbank[:TARGET_LEN]
        fbank = (fbank - MEAN) / (STD * 2)
        mels.append(fbank)
    return torch.stack(mels).unsqueeze(1).to(device)  # [B, 1, T, 128]


def _embed_dim(model_name: str | None = None) -> int:
    """Return embedding dimension for a model name."""
    if model_name is None:
        model_name = _DEFAULT_EMBED_MODEL
    return _EMBED_MODELS.get(model_name, 768)


def _ml_config(model_name: str) -> tuple[str, list[int]] | None:
    """If model_name is a multi-layer variant, return (base_model, layer_indices).

    Returns None for single-layer models.
    Layer indices are 0-based into the list returned by extract_features().
    """
    if not model_name.endswith("_ML"):
        return None
    base = model_name[:-3]  # strip "_ML"
    if base not in _EMBED_MODELS:
        return None
    # Layer counts per model family
    layer_counts = {
        "WAV2VEC2_BASE": 12, "WAV2VEC2_LARGE": 24, "WAV2VEC2_LARGE_LV60K": 24,
        "HUBERT_BASE": 12, "HUBERT_LARGE": 24, "HUBERT_XLARGE": 48,
        "AST": 12,
    }
    n = layer_counts.get(base)
    if n is None:
        return None
    # Select 4 layers at quartile boundaries (0-indexed)
    indices = [n // 4 - 1, n // 2 - 1, 3 * n // 4 - 1, n - 1]
    return base, indices


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


def _w2v_cache_exists(video_path: str, hop: float, window: float,
                      model_name: str | None = None) -> bool:
    """Check if embedding cache exists for a video."""
    try:
        path = _w2v_cache_path(video_path, hop, window, model_name)
        return os.path.exists(path)
    except Exception:
        return False


def _w2v_cache_load(video_path: str, hop: float, window: float,
                    model_name: str | None = None) -> tuple[np.ndarray, np.ndarray] | None:
    """Load embeddings from cache. Returns (timestamps, embeddings) or None."""
    try:
        path = _w2v_cache_path(video_path, hop, window, model_name)
        if os.path.exists(path):
            data = np.load(path)
            _log(f"audio_scan: cache hit ({path})")
            return data["timestamps"], data["embeddings"]
    except Exception as e:
        _log(f"audio_scan: cache read failed: {e}")
    return None


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
    is_ast = (model_name or _DEFAULT_EMBED_MODEL) in ("AST", "AST_ML")
    is_eat = (model_name or _DEFAULT_EMBED_MODEL) == "EAT"
    ml_cfg = _ml_config(model_name or _DEFAULT_EMBED_MODEL)
    # Auto-size batches based on available GPU memory
    batch_size = 16
    if device == "cuda":
        try:
            vram_gb = torch.cuda.get_device_properties(0).total_mem / 1e9
            if vram_gb >= 16:
                batch_size = 64
            elif vram_gb >= 8:
                batch_size = 32
            _log(f"audio_scan: batch_size={batch_size} (VRAM {vram_gb:.1f} GB)")
        except Exception:
            pass
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
                batch_emb = features.mean(dim=1).cpu().numpy()
            elif is_ast:
                inputs = _ast_feature_extractor(
                    list(chunks), sampling_rate=sr, return_tensors="pt",
                    padding=True,
                )
                input_values = inputs.input_values.to(device)
                if ml_cfg is not None:
                    out = model(input_values, output_hidden_states=True)
                    selected = [out.hidden_states[i].mean(dim=1) for i in ml_cfg[1]]
                    batch_emb = torch.cat(selected, dim=1).cpu().numpy()
                else:
                    out = model(input_values)
                    batch_emb = out.last_hidden_state.mean(dim=1).cpu().numpy()
            elif is_eat:
                mel_input = _eat_preprocess(chunks, sr, device)
                features = model.extract_features(mel_input)
                batch_emb = features[:, 1:, :].mean(dim=1).cpu().numpy()
            elif ml_cfg is not None:
                all_layers, _ = model.extract_features(waveforms)
                selected = [all_layers[i].mean(dim=1) for i in ml_cfg[1]]
                batch_emb = torch.cat(selected, dim=1).cpu().numpy()
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
                          gt_negative: list[float] | None = None,
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

    # Manual negative windows: near explicit negative markers
    manual_neg_times = set()
    if gt_negative:
        for gt in gt_negative:
            for offset in range(-int(tolerance), int(tolerance) + 1):
                t = gt + offset
                if 0 <= t <= duration - _WINDOW:
                    manual_neg_times.add(int(t))
        # Don't let manual negatives overlap with positives
        manual_neg_times -= pos_times

    # Auto negative windows: every 4s, far from any marker (skip if margin <= 0 or no markers)
    neg_times = set()
    if all_gt and neg_margin > 0:
        for t in range(0, int(duration - _WINDOW), 4):
            if min(abs(t - g) for g in all_gt) > neg_margin:
                neg_times.add(t)

    all_times = sorted(pos_times | neg_times | manual_neg_times)
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
    is_ast = (model_name or _DEFAULT_EMBED_MODEL) in ("AST", "AST_ML")
    is_eat = (model_name or _DEFAULT_EMBED_MODEL) == "EAT"
    ml_cfg = _ml_config(model_name or _DEFAULT_EMBED_MODEL)

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
                batch_emb = features.mean(dim=1).cpu().numpy()
            elif is_ast:
                inputs = _ast_feature_extractor(
                    list(chunks), sampling_rate=sr, return_tensors="pt",
                    padding=True,
                )
                input_values = inputs.input_values.to(device)
                if ml_cfg is not None:
                    out = model(input_values, output_hidden_states=True)
                    selected = [out.hidden_states[i].mean(dim=1) for i in ml_cfg[1]]
                    batch_emb = torch.cat(selected, dim=1).cpu().numpy()
                else:
                    out = model(input_values)
                    batch_emb = out.last_hidden_state.mean(dim=1).cpu().numpy()
            elif is_eat:
                mel_input = _eat_preprocess(chunks, sr, device)
                features = model.extract_features(mel_input)
                batch_emb = features[:, 1:, :].mean(dim=1).cpu().numpy()
            elif ml_cfg is not None:
                all_layers, _ = model.extract_features(waveforms)
                selected = [all_layers[i].mean(dim=1) for i in ml_cfg[1]]
                batch_emb = torch.cat(selected, dim=1).cpu().numpy()
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
        dm = min((abs(t - g) for g in (gt_negative or [])), default=9999)
        if di < tolerance:
            labels[i] = 1
        elif dm < tolerance or (neg_margin > 0 and da > neg_margin):
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
                     cancel_flag: object = None,
                     n_workers: int = 4,
                     progress_cb: object = None) -> dict:
    """Train a classifier from labeled videos.

    Args:
        video_infos: list of (video_path, intense_times, soft_times)
        model_path: if given, save model to this path
        tolerance/neg_margin: labeling parameters
        embed_model: embedding model name (e.g. "HUBERT_BASE", "BEATS"), defaults to WAV2VEC2_BASE
        cancel_flag: object with _cancel attribute; if set, training aborts early
        n_workers: number of threads for parallel audio loading

    Returns:
        dict with 'classifier', 'embed_model', and metadata, or None on failure.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from sklearn.ensemble import HistGradientBoostingClassifier

    def _progress(msg: str) -> None:
        _log(msg)
        if progress_cb:
            progress_cb(msg)

    def _load_audio(path: str) -> np.ndarray:
        return _load_audio_ffmpeg(path, sr=_SR)

    # Phase 1: load all audio in parallel (cap workers — disk I/O bound)
    n = len(video_infos)
    load_workers = min(n_workers, 4)
    _progress(f"Loading audio: 0/{n} videos ({load_workers} workers)...")
    audio_data: dict[int, np.ndarray] = {}
    with ThreadPoolExecutor(max_workers=load_workers) as pool:
        future_to_idx = {
            pool.submit(_load_audio, vi[0]): i
            for i, vi in enumerate(video_infos)
        }
        failed = set()
        for future in as_completed(future_to_idx):
            if cancel_flag and getattr(cancel_flag, '_cancel', False):
                _log("audio_scan: training cancelled")
                return None
            idx = future_to_idx[future]
            try:
                audio_data[idx] = future.result()
            except Exception as e:
                _log(f"audio_scan: failed to load {os.path.basename(video_infos[idx][0])}: {e}")
                failed.add(idx)
            _progress(f"Loading audio: {len(audio_data) + len(failed)}/{n}")

    # Phase 2: extract embeddings sequentially on GPU
    _progress(f"Extracting embeddings: 0/{n}")
    all_X, all_y = [], []
    for vi, vinfo in enumerate(video_infos):
        if vi in failed:
            continue
        vpath, gt_intense, gt_soft = vinfo[0], vinfo[1], vinfo[2]
        gt_negative = vinfo[3] if len(vinfo) > 3 else []
        if cancel_flag and getattr(cancel_flag, '_cancel', False):
            _log("audio_scan: training cancelled")
            return None
        _progress(f"Extracting embeddings: {vi+1}/{n}")
        y = audio_data.pop(vi)

        timestamps, embeddings, labels = _extract_w2v_targeted(
            y, _SR, gt_intense, gt_soft, tolerance, neg_margin,
            model_name=embed_model, gt_negative=gt_negative,
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

    _progress(f"Fitting classifier on {len(train_idx)} samples...")
    clf = HistGradientBoostingClassifier(
        max_iter=200, max_depth=5, learning_rate=0.1, random_state=42,
    )
    clf.fit(X[train_idx], y_arr[train_idx])
    _log("audio_scan: classifier trained")

    # Calibrate probabilities for better threshold behavior
    from sklearn.calibration import CalibratedClassifierCV
    min_class = min(int(n_pos), int(n_neg_sample))
    if min_class >= 6:
        cal_clf = CalibratedClassifierCV(clf, cv=3, method='isotonic')
        cal_clf.fit(X[train_idx], y_arr[train_idx])
        clf = cal_clf
        _log("audio_scan: classifier calibrated (isotonic, 3-fold)")
    else:
        _log(f"audio_scan: skipping calibration (min class size {min_class} < 6)")

    model = {"classifier": clf, "n_features": X.shape[1],
             "embed_model": embed_model or _DEFAULT_EMBED_MODEL}

    if model_path:
        import joblib
        from datetime import datetime
        parent = os.path.dirname(model_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Save with timestamp in name; keep a symlink/copy as the "latest"
        stem, ext = os.path.splitext(model_path)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        versioned = f"{stem}_{ts}{ext}"
        joblib.dump(model, versioned)
        _log(f"audio_scan: model saved to {versioned}")
        # Update the base path to point to latest version (copy)
        import shutil
        shutil.copy2(versioned, model_path)
        _log(f"audio_scan: latest model updated: {model_path}")

    return model


def load_classifier(model_path: str) -> dict | None:
    """Load a saved classifier model."""
    if not os.path.exists(model_path):
        return None
    import joblib
    return joblib.load(model_path)


def default_model_path(profile_name: str = "default",
                       embed_model: str | None = None) -> str:
    """Return the path for a profile's classifier model.

    When embed_model is given the file is ``{profile}_{model}.joblib``,
    otherwise ``{profile}.joblib`` (legacy single-model layout).
    """
    if embed_model:
        return os.path.join(_MODEL_DIR, f"{profile_name}_{embed_model}.joblib")
    return os.path.join(_MODEL_DIR, f"{profile_name}.joblib")


def list_model_versions(profile_name: str = "default",
                        embed_model: str | None = None) -> list[tuple[str, str]]:
    """Return available backup versions for a model, newest first.

    Returns list of (timestamp_label, file_path).
    The current (active) model is listed first as "current".
    """
    import re
    current = default_model_path(profile_name, embed_model)
    stem, ext = os.path.splitext(current)
    versions: list[tuple[str, str]] = []
    if os.path.exists(current):
        versions.append(("current", current))
    if not os.path.isdir(_MODEL_DIR):
        return versions
    pattern = re.compile(re.escape(os.path.basename(stem)) + r"_(\d{8}_\d{6})" + re.escape(ext) + "$")
    for fname in os.listdir(_MODEL_DIR):
        m = pattern.match(fname)
        if m:
            versions.append((m.group(1), os.path.join(_MODEL_DIR, fname)))
    # Sort backups newest first (after "current")
    current_entry = versions[:1]
    backups = sorted(versions[1:], key=lambda v: v[0], reverse=True)
    return current_entry + backups


def restore_model_version(version_path: str, profile_name: str = "default",
                          embed_model: str | None = None) -> None:
    """Restore a backup version as the active model."""
    import filecmp, shutil
    from datetime import datetime
    current = default_model_path(profile_name, embed_model)
    if version_path == current:
        return
    # Back up current before replacing — but only if no identical backup exists
    if os.path.exists(current):
        stem, ext = os.path.splitext(current)
        already_saved = False
        if os.path.isdir(_MODEL_DIR):
            import re
            pat = re.compile(re.escape(os.path.basename(stem)) + r"_\d{8}_\d{6}" + re.escape(ext) + "$")
            for fname in os.listdir(_MODEL_DIR):
                if pat.match(fname):
                    candidate = os.path.join(_MODEL_DIR, fname)
                    if filecmp.cmp(current, candidate, shallow=False):
                        already_saved = True
                        break
        if not already_saved:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.move(current, f"{stem}_{ts}{ext}")
    shutil.copy2(version_path, current)
    _log(f"audio_scan: restored {os.path.basename(version_path)} as active model")


def list_trained_models(profile_name: str = "default") -> list[str]:
    """Return embedding model names that have a trained .joblib for *profile_name*.

    Looks for files matching ``{profile}_{MODEL}.joblib`` in the models dir.
    """
    prefix = f"{profile_name}_"
    suffix = ".joblib"
    result = []
    if not os.path.isdir(_MODEL_DIR):
        return result
    for fname in os.listdir(_MODEL_DIR):
        if fname.startswith(prefix) and fname.endswith(suffix):
            model_name = fname[len(prefix):-len(suffix)]
            if model_name in _EMBED_MODELS:
                result.append(model_name)
    # Also check legacy {profile}.joblib
    legacy = os.path.join(_MODEL_DIR, f"{profile_name}.joblib")
    if os.path.exists(legacy) and not result:
        # Legacy model — we don't know the embed model, but it's usable
        result.append("")
    return sorted(result)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _fuse_regions(regions: list[tuple[float, float, float]]
                  ) -> list[tuple[float, float, float]]:
    """Merge overlapping/adjacent regions, keeping max score."""
    if not regions:
        return []
    by_start = sorted(regions, key=lambda r: r[0])
    fused: list[tuple[float, float, float]] = []
    s, e, sc = by_start[0]
    for s2, e2, sc2 in by_start[1:]:
        if s2 <= e:  # overlapping or touching
            e = max(e, e2)
            sc = max(sc, sc2)
        else:
            fused.append((s, e, sc))
            s, e, sc = s2, e2, sc2
    fused.append((s, e, sc))
    return fused


def prefetch_audio(video_path: str, embed_model: str | None = None,
                    hop: float = 1.0, window: float = _WINDOW) -> np.ndarray | None:
    """Pre-load audio for a video if embeddings aren't cached.

    Returns the raw audio array, or None if cache already exists.
    Call from a background thread while the GPU is busy with another video.
    """
    if _w2v_cache_exists(video_path, hop, window, embed_model):
        return None
    _log(f"audio_scan: prefetching {os.path.basename(video_path)}")
    y = _load_audio_ffmpeg(video_path, sr=_SR)
    _log(f"audio_scan: prefetched {len(y)/_SR:.1f}s")
    return y


def scan_video(
    video_path: str,
    model: dict = None,
    threshold: float = 0.30,
    hop: float = 1.0,
    window: float = _WINDOW,
    cancel_flag: object = None,
    prefetched_audio: np.ndarray | None = None,
) -> list[tuple[float, float, float]]:
    """Scan a video for matching audio regions using a trained classifier.

    Returns list of (start_time, end_time, score) above threshold.
    If prefetched_audio is provided, skips the ffmpeg decode step.
    """
    if model is None:
        _log("audio_scan: no model provided")
        return []

    clf = model["classifier"]
    embed_model = model.get("embed_model")

    # Try cache first — skip expensive audio loading if embeddings exist
    cached = _w2v_cache_load(video_path, hop, window, embed_model)
    if cached is not None:
        timestamps, window_vectors = cached
    else:
        if prefetched_audio is not None:
            _log(f"audio_scan: using prefetched audio")
            y = prefetched_audio
        else:
            _log(f"audio_scan: loading {video_path}")
            y = _load_audio_ffmpeg(video_path, sr=_SR)
        sr = _SR
        _log(f"audio_scan: {len(y)/sr:.1f}s loaded")

        if cancel_flag and getattr(cancel_flag, '_cancel', False):
            return []

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
    raw = [
        (timestamps[i], timestamps[i] + window, float(probs[i]))
        for i in np.nonzero(mask)[0]
    ]
    results = _fuse_regions(raw)
    _log(f"audio_scan: {len(results)} regions above threshold {threshold} (from {len(raw)} raw)")
    return results
