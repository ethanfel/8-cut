# Audio Pipeline Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Improve audio scan accuracy with multi-layer extraction, AST/EAT models, and calibrated classifier.

**Architecture:** All changes are in `core/audio_scan.py`. The embedding extraction functions gain new model-type branches (AST, EAT, multi-layer). The classifier gets a calibration wrapper. `_EMBED_MODELS` dict and `_get_w2v_model()` are extended. No UI changes needed — new models appear automatically in dropdowns.

**Tech Stack:** torchaudio (existing), transformers (new dep), timm (new dep), sklearn.calibration (existing dep)

**Key design notes:**
- `_get_w2v_model()` resolves `_ML` suffixed names to their base model for loading (e.g. `HUBERT_XLARGE_ML` loads `HUBERT_XLARGE`). Both share the same GPU model — only the extraction path differs (last-layer vs multi-layer). The global `_w2v_model_name` stores the **base** name so switching between `HUBERT_XLARGE` and `HUBERT_XLARGE_ML` does NOT trigger a reload.
- Cache keys use the **full** model name (including `_ML`), so single-layer and multi-layer caches coexist as separate `.npz` files.
- AST and EAT are separate model types that do NOT share the torchaudio loading path — they get their own `elif` branches in `_get_w2v_model()`.
- Both `_extract_w2v_windows` and `_extract_w2v_targeted` need identical changes to their batch inference blocks. Keep them in sync.

---

### Task 1: Add transformers and timm to requirements

**Files:**
- Modify: `requirements.txt`

**Step 1: Add dependencies**

Add after the `torchaudio` line in `requirements.txt`:

```
transformers>=4.30
timm>=0.9
```

**Step 2: Verify install**

Run: `pip install transformers timm`

**Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: add transformers and timm for AST/EAT models"
```

---

### Task 2: Multi-layer extraction for torchaudio models

**Files:**
- Modify: `core/audio_scan.py:50-58` (_EMBED_MODELS dict)
- Modify: `core/audio_scan.py:96-100` (_embed_dim)
- Modify: `core/audio_scan.py:68-93` (_get_w2v_model)
- Modify: `core/audio_scan.py:189-205` (_extract_w2v_windows batch loop)
- Modify: `core/audio_scan.py:278-293` (_extract_w2v_targeted batch loop)
- Test: `tests/test_audio_scan.py`

**Step 1: Write failing test**

Add to `tests/test_audio_scan.py`:

```python
def test_embed_dim_multi_layer():
    from core.audio_scan import _embed_dim
    # Multi-layer models should report concatenated dimension
    assert _embed_dim("HUBERT_XLARGE_ML") == 5120
    assert _embed_dim("HUBERT_LARGE_ML") == 4096
    assert _embed_dim("HUBERT_BASE_ML") == 3072
    # Single-layer unchanged
    assert _embed_dim("HUBERT_XLARGE") == 1280
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_audio_scan.py::test_embed_dim_multi_layer -v`
Expected: FAIL — `_embed_dim("HUBERT_XLARGE_ML")` returns 768 (default fallback)

**Step 3: Add multi-layer entries to _EMBED_MODELS**

In `core/audio_scan.py:50-58`, add after existing entries:

```python
_EMBED_MODELS = {
    "WAV2VEC2_BASE":           768,
    "WAV2VEC2_LARGE":         1024,
    "WAV2VEC2_LARGE_LV60K":  1024,
    "HUBERT_BASE":             768,
    "HUBERT_LARGE":           1024,
    "HUBERT_XLARGE":          1280,
    "BEATS":                   768,
    # Multi-layer variants (4 quartile layers concatenated)
    "WAV2VEC2_BASE_ML":       3072,   # 768 * 4
    "HUBERT_BASE_ML":         3072,   # 768 * 4
    "HUBERT_LARGE_ML":        4096,   # 1024 * 4
    "HUBERT_XLARGE_ML":       5120,   # 1280 * 4
}
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_audio_scan.py::test_embed_dim_multi_layer -v`
Expected: PASS

**Step 5: Add helper to resolve base model and layer indices**

Add after `_embed_dim()` (around line 101):

```python
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
```

Note: AST is included in the layer_counts dict here already so Task 3 doesn't need to modify it again.

**Step 6: Write test for _ml_config**

```python
def test_ml_config():
    from core.audio_scan import _ml_config
    assert _ml_config("HUBERT_XLARGE") is None
    assert _ml_config("BEATS_ML") is None  # BEATS has no ML variant
    base, layers = _ml_config("HUBERT_XLARGE_ML")
    assert base == "HUBERT_XLARGE"
    assert layers == [11, 23, 35, 47]
    base, layers = _ml_config("HUBERT_BASE_ML")
    assert base == "HUBERT_BASE"
    assert layers == [2, 5, 8, 11]
```

Run: `pytest tests/test_audio_scan.py::test_ml_config -v`
Expected: PASS

**Step 7: Modify _get_w2v_model to resolve ML base names**

In `_get_w2v_model()` (line 68), the comparison key must use the resolved base name so that `HUBERT_XLARGE` and `HUBERT_XLARGE_ML` share the same loaded model without reloading:

```python
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
            ...  # existing BEATs code unchanged
        else:
            import torchaudio
            bundle = getattr(torchaudio.pipelines, load_name)
            _w2v_model = bundle.get_model().to(_w2v_device)
        _w2v_model.eval()
        _w2v_model_name = load_name
        _log(f"audio_scan: {load_name} loaded on {_w2v_device}")
    return _w2v_model, _w2v_device
```

**Step 8: Modify _extract_w2v_windows batch inference**

In `_extract_w2v_windows`, compute `ml_cfg` **once** before the batch loop (after line 173 `is_beats = ...`):

```python
    ml_cfg = _ml_config(model_name or _DEFAULT_EMBED_MODEL)
```

Then replace the batch inference block (lines 197-204):

```python
        with torch.no_grad():
            waveforms = torch.from_numpy(np.stack(chunks)).float().to(device)
            if is_beats:
                padding_mask = torch.zeros_like(waveforms, dtype=torch.bool)
                features, _ = model.extract_features(waveforms, padding_mask=padding_mask)
                batch_emb = features.mean(dim=1).cpu().numpy()
            elif ml_cfg is not None:
                all_layers, _ = model.extract_features(waveforms)
                selected = [all_layers[i].mean(dim=1) for i in ml_cfg[1]]
                batch_emb = torch.cat(selected, dim=1).cpu().numpy()
            else:
                features, _ = model(waveforms)
                batch_emb = features.mean(dim=1).cpu().numpy()
        embeddings.append(batch_emb)
```

**Step 9: Modify _extract_w2v_targeted batch inference (keep in sync)**

In `_extract_w2v_targeted`, add `ml_cfg` computation after line 276 `is_beats = ...`:

```python
    ml_cfg = _ml_config(model_name or _DEFAULT_EMBED_MODEL)
```

Then replace the batch inference block (lines 285-292) with the same branching logic as Step 8:

```python
        with torch.no_grad():
            waveforms = torch.from_numpy(np.stack(chunks)).float().to(device)
            if is_beats:
                padding_mask = torch.zeros_like(waveforms, dtype=torch.bool)
                features, _ = model.extract_features(waveforms, padding_mask=padding_mask)
                batch_emb = features.mean(dim=1).cpu().numpy()
            elif ml_cfg is not None:
                all_layers, _ = model.extract_features(waveforms)
                selected = [all_layers[i].mean(dim=1) for i in ml_cfg[1]]
                batch_emb = torch.cat(selected, dim=1).cpu().numpy()
            else:
                features, _ = model(waveforms)
                batch_emb = features.mean(dim=1).cpu().numpy()
        embeddings_list.append(batch_emb)
```

Note: `_extract_w2v_targeted` appends to `embeddings_list` (not `embeddings`).

**Step 10: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass

**Step 11: Commit**

```bash
git add core/audio_scan.py tests/test_audio_scan.py
git commit -m "feat: multi-layer extraction for HuBERT/Wav2Vec2 models"
```

---

### Task 3: AST model integration

**Files:**
- Modify: `core/audio_scan.py:50-65` (_EMBED_MODELS, add AST entries)
- Modify: `core/audio_scan.py:45-47` (add _ast_feature_extractor global)
- Modify: `core/audio_scan.py:68-93` (_get_w2v_model, add AST loading branch)
- Modify: `core/audio_scan.py` (_extract_w2v_windows and _extract_w2v_targeted, add AST inference branch)
- Test: `tests/test_audio_scan.py`

**Step 1: Write failing test**

```python
def test_embed_dim_ast():
    from core.audio_scan import _embed_dim
    assert _embed_dim("AST") == 768
    assert _embed_dim("AST_ML") == 3072
```

Run: `pytest tests/test_audio_scan.py::test_embed_dim_ast -v`
Expected: FAIL

**Step 2: Add AST entries to _EMBED_MODELS**

Add to the dict (after the ML entries):

```python
    # Transformers-based models
    "AST":                     768,
    "AST_ML":                 3072,   # 768 * 4
```

Run test again — should PASS now.

**Step 3: Add module-level global for AST feature extractor**

Near line 47 (after `_w2v_model_name = None`):

```python
_ast_feature_extractor = None
```

**Step 4: Add AST loading branch in _get_w2v_model**

In `_get_w2v_model()`, add an `elif` branch **before** the torchaudio fallback `else`:

```python
        elif load_name == "AST":
            from transformers import ASTModel, ASTFeatureExtractor
            _w2v_model = ASTModel.from_pretrained(
                "MIT/ast-finetuned-audioset-10-10-0.4593"
            ).to(_w2v_device)
            global _ast_feature_extractor
            _ast_feature_extractor = ASTFeatureExtractor.from_pretrained(
                "MIT/ast-finetuned-audioset-10-10-0.4593"
            )
```

Note: `_ast_feature_extractor` is recreated on every model load (not cached separately) — simple and correct since the feature extractor is lightweight and model reloads are rare.

**Step 5: Add AST inference branch in both extraction functions**

In both `_extract_w2v_windows` and `_extract_w2v_targeted`, compute `is_ast` once before the loop:

```python
    is_ast = (model_name or _DEFAULT_EMBED_MODEL) in ("AST", "AST_ML")
```

Then in the batch inference block, add after the `elif ml_cfg` branch and before `else`:

```python
            elif is_ast:
                # AST uses its own feature extractor for mel spectrogram
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
```

Important: `chunks` is already a list of numpy arrays (built in the loop at lines 194-196). Pass it directly as `list(chunks)` — the `ASTFeatureExtractor` accepts a list of numpy arrays and handles batching/padding internally. Verified: `ASTFeatureExtractor([np.array, np.array, ...], sampling_rate=16000, return_tensors="pt", padding=True)` returns `input_values` of shape `[B, 1024, 128]`.

**Step 6: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass

**Step 7: Commit**

```bash
git add core/audio_scan.py tests/test_audio_scan.py
git commit -m "feat: add AST (Audio Spectrogram Transformer) embedding model"
```

---

### Task 4: EAT model integration

**Files:**
- Modify: `core/audio_scan.py:50-65` (_EMBED_MODELS, add EAT entry)
- Modify: `core/audio_scan.py:68-93` (_get_w2v_model, add EAT loading branch)
- Add: `core/audio_scan.py` (_eat_preprocess helper function)
- Modify: `core/audio_scan.py` (_extract_w2v_windows and _extract_w2v_targeted, add EAT inference branch)
- Test: `tests/test_audio_scan.py`

**Step 1: Write failing test**

```python
def test_embed_dim_eat():
    from core.audio_scan import _embed_dim
    assert _embed_dim("EAT") == 768
```

**Step 2: Add EAT entry to _EMBED_MODELS**

```python
    "EAT":                     768,
```

Note: No `EAT_ML` variant — EAT's `extract_features()` does not natively support multi-layer output. Can be added later if needed by monkey-patching.

**Step 3: Add EAT loading branch in _get_w2v_model**

Add after the AST branch, before the torchaudio `else`:

```python
        elif load_name == "EAT":
            from transformers import AutoModel
            _w2v_model = AutoModel.from_pretrained(
                "worstchan/EAT-base_epoch30_finetune_AS2M",
                trust_remote_code=True,
            ).to(_w2v_device)
```

**Step 4: Add EAT preprocessing helper**

Add as a module-level function near `_get_w2v_model`:

```python
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
```

**Step 5: Add EAT inference branch in both extraction functions**

Compute `is_eat` once before the loop:

```python
    is_eat = (model_name or _DEFAULT_EMBED_MODEL) == "EAT"
```

Then in the batch inference block, add after the `elif is_ast` branch and before `else`:

```python
            elif is_eat:
                mel_input = _eat_preprocess(chunks, sr, device)
                features = model.extract_features(mel_input)
                # Mean-pool frame-level tokens (skip CLS at index 0)
                batch_emb = features[:, 1:, :].mean(dim=1).cpu().numpy()
```

Important: `model.extract_features()` returns a plain `torch.Tensor` of shape `[B, 513, 768]` (not a tuple). Index 0 is the CLS token, indices 1-512 are frame-level patch embeddings. We mean-pool the frame tokens for consistency with how other models are pooled.

**Step 6: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass

**Step 7: Commit**

```bash
git add core/audio_scan.py tests/test_audio_scan.py
git commit -m "feat: add EAT (Efficient Audio Transformer) embedding model"
```

---

### Task 5: Calibrated classifier

**Files:**
- Modify: `core/audio_scan.py:424-429` (train_classifier, wrap clf)
- Test: `tests/test_audio_scan.py`

**Step 1: Modify train_classifier**

After the existing `clf.fit()` call (line 428), add calibration with a safe guard:

```python
    clf.fit(X[train_idx], y_arr[train_idx])
    _log("audio_scan: classifier trained")

    # Calibrate probabilities for better threshold behavior
    # Requires at least 6 samples per class for stable 3-fold isotonic calibration
    from sklearn.calibration import CalibratedClassifierCV
    min_class = min(int(n_pos), int(n_neg_sample))
    if min_class >= 6:
        cal_clf = CalibratedClassifierCV(clf, cv=3, method='isotonic')
        cal_clf.fit(X[train_idx], y_arr[train_idx])
        clf = cal_clf
        _log("audio_scan: classifier calibrated (isotonic, 3-fold)")
    else:
        _log(f"audio_scan: skipping calibration (min class size {min_class} < 6)")
```

Why `min_class >= 6`: `CalibratedClassifierCV` uses stratified k-fold internally. With `cv=3`, each fold needs at least 2 samples per class. `min_class >= 6` guarantees this. With fewer samples, the uncalibrated HistGBT probabilities are still reasonable — calibration is an enhancement, not a requirement.

Previous plan bug: `cv=min(3, n_pos, n_neg_sample)` could produce `cv=1` when `n_pos=1`, which raises `ValueError` (minimum is 2). Even `cv=2` with 2 positives causes one fold to have only 1 positive, making isotonic regression unstable. The `>= 6` guard avoids all these edge cases.

**Step 2: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass

**Step 3: Commit**

```bash
git add core/audio_scan.py
git commit -m "feat: calibrate classifier probabilities with isotonic regression"
```

---

### Task 6: Integration test with real model (manual)

This task is manual — it requires GPU and a real video file.

**Step 1: Test multi-layer extraction**

```bash
python -c "
from core.audio_scan import _extract_w2v_windows, _embed_dim
import numpy as np
y = np.random.randn(16000 * 20).astype(np.float32) * 0.01
ts, emb = _extract_w2v_windows(y, model_name='HUBERT_XLARGE_ML')
print(f'HUBERT_XLARGE_ML: {emb.shape}')  # expect (13, 5120)
assert emb.shape[1] == _embed_dim('HUBERT_XLARGE_ML')
print('PASS')
"
```

**Step 2: Test AST extraction**

```bash
python -c "
from core.audio_scan import _extract_w2v_windows, _embed_dim
import numpy as np
y = np.random.randn(16000 * 20).astype(np.float32) * 0.01
ts, emb = _extract_w2v_windows(y, model_name='AST')
print(f'AST: {emb.shape}')  # expect (13, 768)
assert emb.shape[1] == _embed_dim('AST')
print('PASS')
"
```

**Step 3: Test AST multi-layer**

```bash
python -c "
from core.audio_scan import _extract_w2v_windows, _embed_dim
import numpy as np
y = np.random.randn(16000 * 20).astype(np.float32) * 0.01
ts, emb = _extract_w2v_windows(y, model_name='AST_ML')
print(f'AST_ML: {emb.shape}')  # expect (13, 3072)
assert emb.shape[1] == _embed_dim('AST_ML')
print('PASS')
"
```

**Step 4: Test EAT extraction**

```bash
python -c "
from core.audio_scan import _extract_w2v_windows, _embed_dim
import numpy as np
y = np.random.randn(16000 * 20).astype(np.float32) * 0.01
ts, emb = _extract_w2v_windows(y, model_name='EAT')
print(f'EAT: {emb.shape}')  # expect (13, 768)
assert emb.shape[1] == _embed_dim('EAT')
print('PASS')
"
```

**Step 5: Test model switching doesn't reload unnecessarily**

```bash
python -c "
from core.audio_scan import _get_w2v_model
import core.audio_scan as m
# Load HUBERT_XLARGE
_get_w2v_model('HUBERT_XLARGE')
name1 = m._w2v_model_name
# Switch to ML variant — should NOT reload
_get_w2v_model('HUBERT_XLARGE_ML')
name2 = m._w2v_model_name
assert name1 == name2 == 'HUBERT_XLARGE', f'Expected no reload, got {name1} -> {name2}'
print('PASS: no reload on ML switch')
"
```

**Step 6: Test full train+scan cycle in app**

Load app, select each new model from scan model dropdown, scan a video, train, verify results display correctly.

**Step 7: Final commit and push**

```bash
git push
```
