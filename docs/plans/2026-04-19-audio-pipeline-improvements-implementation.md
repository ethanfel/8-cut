# Audio Pipeline Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Improve audio scan accuracy with multi-layer extraction, AST/EAT models, and calibrated classifier.

**Architecture:** All changes are in `core/audio_scan.py`. The embedding extraction functions gain new model-type branches (AST, EAT, multi-layer). The classifier gets a calibration wrapper. `_EMBED_MODELS` dict and `_get_w2v_model()` are extended. No UI changes needed — new models appear automatically in dropdowns.

**Tech Stack:** torchaudio (existing), transformers (new dep), timm (new dep), sklearn.calibration (existing dep)

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
    # torchaudio layer counts: BASE=12, LARGE=24, XLARGE=48
    layer_counts = {
        "WAV2VEC2_BASE": 12, "WAV2VEC2_LARGE": 24, "WAV2VEC2_LARGE_LV60K": 24,
        "HUBERT_BASE": 12, "HUBERT_LARGE": 24, "HUBERT_XLARGE": 48,
    }
    n = layer_counts.get(base)
    if n is None:
        return None
    # Select 4 layers at quartile boundaries (1-indexed quartiles, 0-indexed list)
    indices = [n // 4 - 1, n // 2 - 1, 3 * n // 4 - 1, n - 1]
    return base, indices
```

**Step 6: Write test for _ml_config**

```python
def test_ml_config():
    from core.audio_scan import _ml_config
    assert _ml_config("HUBERT_XLARGE") is None
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

In `_get_w2v_model()` (line 68), before loading, strip `_ML` suffix:

```python
def _get_w2v_model(model_name: str | None = None):
    """Lazy-load an embedding model. Reloads if model_name differs from cached."""
    global _w2v_model, _w2v_device, _w2v_model_name
    if model_name is None:
        model_name = _DEFAULT_EMBED_MODEL
    # Multi-layer variants use the same base model
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

**Step 8: Modify extraction to use extract_features for ML models**

In `_extract_w2v_windows` (line 197-204), change the batch inference block:

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

Where `ml_cfg = _ml_config(model_name)` is computed once before the loop.

Apply the same change to `_extract_w2v_targeted` (line 285-292).

**Step 9: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass

**Step 10: Commit**

```bash
git add core/audio_scan.py tests/test_audio_scan.py
git commit -m "feat: multi-layer extraction for HuBERT/Wav2Vec2 models"
```

---

### Task 3: AST model integration

**Files:**
- Modify: `core/audio_scan.py:50-65` (_EMBED_MODELS, add AST entries)
- Modify: `core/audio_scan.py:68-93` (_get_w2v_model, add AST branch)
- Modify: `core/audio_scan.py:189-205` (_extract_w2v_windows, add AST branch)
- Modify: `core/audio_scan.py:278-293` (_extract_w2v_targeted, add AST branch)
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

```python
    "AST":                     768,
    "AST_ML":                 3072,   # 768 * 4
```

**Step 3: Add AST to _ml_config layer counts**

AST has 12 transformer layers + 1 embedding layer = 13 hidden states. Use layers [3, 6, 9, 12] (0-indexed) for quartiles.

```python
    layer_counts = {
        ...existing...
        "AST": 12,
    }
```

**Step 4: Add AST feature extractor cache**

Add module-level globals near existing `_w2v_model`:

```python
_ast_feature_extractor = None
```

**Step 5: Add AST loading branch in _get_w2v_model**

```python
        elif load_name == "AST":
            from transformers import ASTModel
            _w2v_model = ASTModel.from_pretrained(
                "MIT/ast-finetuned-audioset-10-10-0.4593"
            ).to(_w2v_device)
            global _ast_feature_extractor
            if _ast_feature_extractor is None:
                from transformers import ASTFeatureExtractor
                _ast_feature_extractor = ASTFeatureExtractor.from_pretrained(
                    "MIT/ast-finetuned-audioset-10-10-0.4593"
                )
```

**Step 6: Add AST inference branch in extraction functions**

In `_extract_w2v_windows` and `_extract_w2v_targeted`, add a branch for AST models:

```python
            elif is_ast:
                # AST uses its own feature extractor for mel spectrogram
                inputs = _ast_feature_extractor(
                    list(chunks_np), sampling_rate=sr, return_tensors="pt",
                    padding=True,
                )
                input_values = inputs.input_values.to(device)
                out = model(input_values, output_hidden_states=ml_cfg is not None)
                if ml_cfg is not None:
                    selected = [out.hidden_states[i].mean(dim=1) for i in ml_cfg[1]]
                    batch_emb = torch.cat(selected, dim=1).cpu().numpy()
                else:
                    batch_emb = out.last_hidden_state.mean(dim=1).cpu().numpy()
```

Where `is_ast = (model_name or _DEFAULT_EMBED_MODEL) in ("AST", "AST_ML")` and `chunks_np` is the list of raw numpy audio arrays (not stacked tensor).

**Step 7: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass

**Step 8: Commit**

```bash
git add core/audio_scan.py tests/test_audio_scan.py
git commit -m "feat: add AST (Audio Spectrogram Transformer) embedding model"
```

---

### Task 4: EAT model integration

**Files:**
- Modify: `core/audio_scan.py:50-65` (_EMBED_MODELS, add EAT entry)
- Modify: `core/audio_scan.py:68-93` (_get_w2v_model, add EAT branch)
- Modify: `core/audio_scan.py:189-205` (_extract_w2v_windows, add EAT branch)
- Modify: `core/audio_scan.py:278-293` (_extract_w2v_targeted, add EAT branch)
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

**Step 3: Add EAT loading branch in _get_w2v_model**

```python
        elif load_name == "EAT":
            from transformers import AutoModel
            _w2v_model = AutoModel.from_pretrained(
                "worstchan/EAT-base_epoch30_finetune_AS2M",
                trust_remote_code=True,
            ).to(_w2v_device)
```

**Step 4: Add EAT preprocessing helper**

Add near `_get_w2v_model`:

```python
def _eat_preprocess(chunks: list[np.ndarray], sr: int, device: str) -> torch.Tensor:
    """Convert raw audio chunks to EAT mel spectrogram input.
    
    Returns tensor of shape [B, 1, T, 128].
    """
    import torch
    import torchaudio.compliance.kaldi as kaldi

    TARGET_LEN = 1024  # ~10s at 10ms frame shift
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

**Step 5: Add EAT inference branch in extraction functions**

```python
            elif is_eat:
                mel_input = _eat_preprocess(chunks, sr, device)
                features = model.extract_features(mel_input)
                # Mean-pool frame-level tokens (skip CLS at index 0)
                batch_emb = features[:, 1:, :].mean(dim=1).cpu().numpy()
```

Where `is_eat = (model_name or _DEFAULT_EMBED_MODEL) == "EAT"`.

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

After the existing `clf.fit()` call (line 428), add calibration:

```python
    clf.fit(X[train_idx], y_arr[train_idx])
    _log("audio_scan: classifier trained")

    # Calibrate probabilities for better threshold behavior
    from sklearn.calibration import CalibratedClassifierCV
    if len(train_idx) >= 10:
        cal_clf = CalibratedClassifierCV(clf, cv=min(3, n_pos, n_neg_sample),
                                          method='isotonic')
        cal_clf.fit(X[train_idx], y_arr[train_idx])
        clf = cal_clf
        _log("audio_scan: classifier calibrated")
```

The `cv=min(3, n_pos, n_neg_sample)` guard prevents errors when one class has very few samples.

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
"
```

**Step 3: Test EAT extraction**

```bash
python -c "
from core.audio_scan import _extract_w2v_windows, _embed_dim
import numpy as np
y = np.random.randn(16000 * 20).astype(np.float32) * 0.01
ts, emb = _extract_w2v_windows(y, model_name='EAT')
print(f'EAT: {emb.shape}')  # expect (13, 768)
assert emb.shape[1] == _embed_dim('EAT')
"
```

**Step 4: Test full train+scan cycle**

Load app, select HUBERT_XLARGE_ML from scan model dropdown, scan a video, train, verify results display.

**Step 5: Final commit and push**

```bash
git push
```
