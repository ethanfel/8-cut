#!/usr/bin/env python3
"""Calibration — per-video normalized features + classifier."""
import sys, os, time, warnings
sys.path.insert(0, os.path.dirname(__file__))
warnings.filterwarnings("ignore")

import numpy as np
import librosa
from sklearn.ensemble import GradientBoostingClassifier

from core.audio_scan import _SR, _WINDOW

_HOP_LENGTH = 1024
_N_FFT = 2048
from core.db import ProcessedDB

PLEX_DIR = "/media/unraid/appdata/plex/download/porn_jav/"
PROFILE_NAME = "JAV_missionary"
TOLERANCE = 12.0
NEG_MARGIN = 120.0


def extract_rich_features(y, sr=_SR):
    """Per-frame features: onset, energy, spectral shape, mel bands (22 features)."""
    hop = _HOP_LENGTH
    S = np.abs(librosa.stft(y, n_fft=_N_FFT, hop_length=hop)) ** 2
    rms = librosa.feature.rms(S=S, hop_length=hop)
    cent = librosa.feature.spectral_centroid(S=S, sr=sr)
    bw = librosa.feature.spectral_bandwidth(S=S, sr=sr)
    rolloff = librosa.feature.spectral_rolloff(S=S, sr=sr)
    flatness = librosa.feature.spectral_flatness(S=S)
    zcr = librosa.feature.zero_crossing_rate(y, hop_length=hop)
    onset = librosa.onset.onset_strength(S=librosa.power_to_db(S), sr=sr, hop_length=hop).reshape(1, -1)

    mel_S = librosa.feature.melspectrogram(S=S, sr=sr, hop_length=hop, n_mels=128)
    mel_freqs = librosa.mel_frequencies(n_mels=128, fmin=0, fmax=sr/2)
    bands = [(0, 100), (100, 300), (300, 600), (600, 1200),
             (1200, 2000), (2000, 3500), (3500, 5500), (5500, 8000)]
    band_feats = []
    for flo, fhi in bands:
        mask = (mel_freqs >= flo) & (mel_freqs < fhi)
        if mask.sum() > 0:
            band_feats.append(librosa.power_to_db(mel_S[mask].mean(axis=0, keepdims=True) + 1e-10))
        else:
            band_feats.append(np.zeros((1, mel_S.shape[1])))

    sc = librosa.feature.spectral_contrast(S=S, sr=sr, hop_length=hop)

    min_t = min(rms.shape[1], cent.shape[1], onset.shape[1], sc.shape[1],
                band_feats[0].shape[1])
    return np.vstack([
        rms[:, :min_t], cent[:, :min_t], bw[:, :min_t], rolloff[:, :min_t],
        flatness[:, :min_t], zcr[:, :min_t], onset[:, :min_t],
    ] + [b[:, :min_t] for b in band_feats]
    + [sc[:, :min_t]])


def compute_window_stats(feat, hop=1.0):
    """Sliding window mean/std → (timestamps, feature_vectors)."""
    n_feats, T = feat.shape
    fps = _SR / _HOP_LENGTH
    win_frames = int(_WINDOW * fps)
    hop_frames = int(hop * fps)
    if win_frames > T:
        return np.array([]), np.array([])

    cumsum = np.zeros((n_feats, T + 1))
    cumsum[:, 1:] = np.cumsum(feat, axis=1)
    cumsq = np.zeros((n_feats, T + 1))
    cumsq[:, 1:] = np.cumsum(feat ** 2, axis=1)

    starts = np.arange(0, T - win_frames + 1, hop_frames)
    ends = starts + win_frames
    sums = cumsum[:, ends] - cumsum[:, starts]
    sq_sums = cumsq[:, ends] - cumsq[:, starts]
    means = sums / win_frames
    stds = np.sqrt(np.maximum(sq_sums / win_frames - means ** 2, 0) + 1e-10)

    return starts / fps, np.vstack([means, stds]).T


def label_windows(timestamps, gt_intense, gt_soft):
    all_gt = list(gt_intense) + list(gt_soft)
    labels = np.zeros(len(timestamps), dtype=int)
    for i, t in enumerate(timestamps):
        di = min((abs(t - g) for g in gt_intense), default=9999)
        da = min((abs(t - g) for g in all_gt), default=9999)
        if di < TOLERANCE:
            labels[i] = 1
        elif da > NEG_MARGIN:
            labels[i] = -1
    return labels


def main():
    db = ProcessedDB()
    rows = db._con.execute(
        "SELECT filename, start_time, output_path FROM processed WHERE profile = ?",
        (PROFILE_NAME,),
    ).fetchall()

    intense_by_video, soft_by_video = {}, {}
    for fn, st, op in rows:
        if '/mp4_Intense/' in op:
            intense_by_video.setdefault(fn, set()).add(st)
        elif '/mp4_Soft/' in op:
            soft_by_video.setdefault(fn, set()).add(st)

    videos = [fn for fn in intense_by_video
              if os.path.exists(os.path.join(PLEX_DIR, fn))]
    n_vids = int(sys.argv[1]) if len(sys.argv) > 1 else len(videos)
    videos = videos[:n_vids]
    print(f"Processing {len(videos)} videos...")

    all_data_raw = []    # raw features
    all_data_norm = []   # per-video z-scored features

    for vi, vname in enumerate(videos):
        vpath = os.path.join(PLEX_DIR, vname)
        gt_intense = sorted(intense_by_video.get(vname, set()))
        gt_soft = sorted(soft_by_video.get(vname, set()))

        t0 = time.time()
        y, _ = librosa.load(vpath, sr=_SR, mono=True)
        feat = extract_rich_features(y)
        timestamps, window_vectors = compute_window_stats(feat, hop=1.0)
        dt = time.time() - t0

        if len(timestamps) == 0:
            continue

        labels = label_windows(timestamps, gt_intense, gt_soft)

        # Per-video z-score normalization
        vid_mean = window_vectors.mean(axis=0)
        vid_std = window_vectors.std(axis=0)
        vid_std = np.maximum(vid_std, 1e-6)
        normed = (window_vectors - vid_mean) / vid_std

        n_pos = (labels == 1).sum()
        n_neg = (labels == -1).sum()
        print(f"  [{vi+1}/{len(videos)}] {vname[:55]}  pos={n_pos} neg={n_neg} ({dt:.1f}s)")

        all_data_raw.append((vi, vname, timestamps, window_vectors, labels))
        all_data_norm.append((vi, vname, timestamps, normed, labels))

    # Run CV for both raw and normalized
    for label, data in [("RAW features", all_data_raw),
                        ("PER-VIDEO NORMALIZED features", all_data_norm)]:
        print(f"\n{'='*70}")
        print(f"  {label}")
        print(f"{'='*70}")

        all_y_true, all_y_prob = [], []

        for test_idx in range(len(data)):
            _, vname, _, test_X, test_labels = data[test_idx]
            test_mask = test_labels != 0
            if test_mask.sum() == 0 or (test_labels[test_mask] == 1).sum() == 0:
                continue
            X_test = test_X[test_mask]
            y_test = (test_labels[test_mask] == 1).astype(int)

            X_parts, y_parts = [], []
            for i, (_, _, _, feats, labs) in enumerate(data):
                if i == test_idx:
                    continue
                m = labs != 0
                if m.sum() == 0:
                    continue
                X_parts.append(feats[m])
                y_parts.append((labs[m] == 1).astype(int))

            if not X_parts:
                continue
            X_train = np.vstack(X_parts)
            y_train = np.concatenate(y_parts)

            pos_idx = np.where(y_train == 1)[0]
            neg_idx = np.where(y_train == 0)[0]
            if len(pos_idx) == 0 or len(neg_idx) == 0:
                continue
            rng = np.random.RandomState(42)
            n_neg = min(len(neg_idx), len(pos_idx) * 3)
            neg_sample = rng.choice(neg_idx, n_neg, replace=False)
            train_idx = np.concatenate([pos_idx, neg_sample])

            clf = GradientBoostingClassifier(
                n_estimators=200, max_depth=5, learning_rate=0.1, random_state=42
            )
            clf.fit(X_train[train_idx], y_train[train_idx])
            probs = clf.predict_proba(X_test)[:, 1]

            tp = ((probs >= 0.5) & (y_test == 1)).sum()
            fp = ((probs >= 0.5) & (y_test == 0)).sum()
            fn_count = ((probs < 0.5) & (y_test == 1)).sum()
            pos_s = probs[y_test == 1].mean() if (y_test == 1).sum() > 0 else 0
            neg_s = probs[y_test == 0].mean() if (y_test == 0).sum() > 0 else 0
            print(f"  {vname[:50]:50s}  TP={tp:3d} FP={fp:4d} FN={fn_count:3d}  pos_p={pos_s:.3f} neg_p={neg_s:.3f}")

            all_y_true.extend(y_test)
            all_y_prob.extend(probs)

        if not all_y_true:
            print("  No test results.")
            continue

        y_true = np.array(all_y_true)
        y_prob = np.array(all_y_prob)
        pos_probs = y_prob[y_true == 1]
        neg_probs = y_prob[y_true == 0]

        if len(pos_probs) > 0 and len(neg_probs) > 0:
            print(f"\n  POS: 25%={np.percentile(pos_probs,25):.3f} 50%={np.percentile(pos_probs,50):.3f}"
                  f" 75%={np.percentile(pos_probs,75):.3f} max={pos_probs.max():.3f}")
            print(f"  NEG: 25%={np.percentile(neg_probs,25):.3f} 50%={np.percentile(neg_probs,50):.3f}"
                  f" 75%={np.percentile(neg_probs,75):.3f} max={neg_probs.max():.3f}")

        best_f1, best_thr = 0, 0
        print(f"\n  {'thr':>5}  {'prec':>6}  {'recall':>6}  {'TP':>5}  {'FP':>5}  {'FN':>4}  {'F1':>6}")
        for thr in np.arange(0.10, 0.91, 0.05):
            tp = ((y_prob >= thr) & (y_true == 1)).sum()
            fp = ((y_prob >= thr) & (y_true == 0)).sum()
            fn_count = ((y_prob < thr) & (y_true == 1)).sum()
            prec = tp / (tp + fp) if (tp + fp) > 0 else 0
            rec = tp / (tp + fn_count) if (tp + fn_count) > 0 else 0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
            if f1 > best_f1:
                best_f1, best_thr = f1, thr
            print(f"  {thr:.2f}   {prec:.4f}  {rec:.4f}  {tp:5d}  {fp:5d}  {fn_count:4d}  {f1:.4f}")
        print(f"\n  Best F1={best_f1:.4f} at thr={best_thr:.2f}")

        # Feature importance
        X_all = np.vstack([f[l != 0] for _, _, _, f, l in data])
        y_all = np.concatenate([(l[l != 0] == 1).astype(int) for _, _, _, _, l in data])
        pos_idx = np.where(y_all == 1)[0]
        neg_idx = np.where(y_all == 0)[0]
        rng = np.random.RandomState(42)
        neg_sub = rng.choice(neg_idx, min(len(neg_idx), len(pos_idx)*3), replace=False)
        clf = GradientBoostingClassifier(n_estimators=200, max_depth=5, learning_rate=0.1, random_state=42)
        clf.fit(X_all[np.concatenate([pos_idx, neg_sub])], y_all[np.concatenate([pos_idx, neg_sub])])

        feat_names = (
            ["rms", "centroid", "bw", "rolloff", "flat", "zcr", "onset"]
            + [f"mel{i}" for i in range(8)]
            + [f"sc{i}" for i in range(7)]
        )
        stat_names = [f"{f}_m" for f in feat_names] + [f"{f}_s" for f in feat_names]
        imp = clf.feature_importances_
        top = sorted(zip(stat_names, imp), key=lambda x: -x[1])[:10]
        print(f"  Top features: {', '.join(f'{n}={v:.3f}' for n, v in top)}")


if __name__ == "__main__":
    main()
