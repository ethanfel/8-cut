# Audio Pipeline Improvements Design

Date: 2026-04-19

## Goal

Improve audio scan classification accuracy, especially for non-speech sounds (suction, gagging, impacts), through three changes:

1. Multi-layer feature extraction from existing HuBERT/Wav2Vec2 models
2. Two new embedding models: AST (AudioSet-supervised) and EAT (self-supervised + AudioSet finetuned)
3. Calibrated classifier for better threshold behavior

## 1. Multi-Layer Feature Extraction

### Current behavior

`model(waveforms)` extracts embeddings from the **last transformer layer only**.

### Change

Use `model.extract_features(waveforms)` (torchaudio API) to get all layer outputs. Select layers at quartile boundaries, mean-pool each over time, concatenate.

| Model | Layers | Single-layer dim | Multi-layer dim (4 quartiles) |
|-------|--------|-------------------|-------------------------------|
| HUBERT_XLARGE | 48 | 1280 | 5120 |
| HUBERT_LARGE | 24 | 1024 | 4096 |
| HUBERT_BASE | 12 | 768 | 3072 |
| WAV2VEC2_BASE | 12 | 768 | 3072 |

### Implementation

- New entries in `_EMBED_MODELS`: `"HUBERT_XLARGE_ML"` -> 5120, etc.
- `_extract_w2v_windows`: when model name ends with `_ML`, call `extract_features()` instead of `model()`, select quartile layers, concat
- Cache key: model name includes `_ML` suffix -> separate cache files
- No change to classifier or training pipeline (HistGBT handles high-dim fine)

## 2. AST (Audio Spectrogram Transformer)

### What

`MIT/ast-finetuned-audioset-10-10-0.4593` via HuggingFace `transformers`. 86M params, 768-dim, supervised on AudioSet 527 sound classes.

### Integration

- Load: `ASTModel.from_pretrained()` + `ASTFeatureExtractor`
- Preprocessing: `ASTFeatureExtractor` handles mel spectrogram from 16kHz raw audio
- Batching: prepare `input_values` per window, stack into batch, forward through model
- Multi-layer: `output_hidden_states=True` returns 13 layers; `AST_ML` variant concats quartile layers -> 3072-dim
- Model cached via `_get_w2v_model()` same lazy-load pattern

### Entries

- `"AST"` -> 768
- `"AST_ML"` -> 3072

## 3. EAT (Efficient Audio Transformer)

### What

`worstchan/EAT-base_epoch30_finetune_AS2M` via HuggingFace with `trust_remote_code=True`. 88M params, 768-dim, self-supervised + AudioSet finetuned.

### Integration

- Load: `AutoModel.from_pretrained(..., trust_remote_code=True)`
- Preprocessing: manual 128-bin Kaldi fbank mel spectrogram via torchaudio, normalize with EAT constants `(mel - (-4.268)) / (4.569 * 2)`, reshape to `[B, 1, T, 128]`
- Feature extraction: `model.extract_features(mel)` returns `[B, seq, 768]`; CLS token `[:, 0, :]` for utterance-level, or mean-pool `[:, 1:, :]` for frame-level. Use mean-pool for consistency with other models.
- Multi-layer: not natively supported, skip for now

### Entry

- `"EAT"` -> 768

## 4. Calibrated Classifier

Wrap `HistGradientBoostingClassifier` in `CalibratedClassifierCV(clf, cv=3, method='isotonic')` after fitting. Gives well-calibrated probabilities -> threshold slider maps more linearly to precision/recall.

One change in `train_classifier()`, no UI changes needed.

## 5. Requirements

Add to `requirements.txt`:
```
transformers>=4.30
timm>=0.9
```

Both AST and EAT need `transformers`. EAT additionally needs `timm` (used internally by its custom model code). Both setup scripts (`setup_env.sh`, `setup-windows.ps1`) install from `requirements.txt` so no changes needed there.

## Cache Compatibility

- All new model variants get distinct cache keys via model name in the hash
- Existing caches for HUBERT_XLARGE, BEATs, etc. remain valid and untouched
- New models create new `.npz` files in the same `cache/w2v/` directory

## UI Changes

- `_EMBED_MODELS` dict additions appear automatically in Train dialog model dropdown and scan model dropdown
- No other UI changes needed
