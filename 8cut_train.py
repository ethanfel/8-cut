#!/usr/bin/env python3
"""Train an audio scan classifier from DB ground truth.

Usage:
    python 8cut_train.py                                    # default model, auto-detect positive
    python 8cut_train.py --model BEATS                      # specific embedding model
    python 8cut_train.py --positive mp4_Intense                 # explicit positive folder
    python 8cut_train.py --positive mp4_Intense --model BEATS   # both
"""
import sys, os, warnings
sys.path.insert(0, os.path.dirname(__file__))
warnings.filterwarnings("ignore")

from core.audio_scan import train_classifier, default_model_path, _EMBED_MODELS
from core.db import ProcessedDB

PROFILE_NAME = "JAV_missionary"

# Fallback for old DB rows without source_path
PLEX_DIR = "/media/unraid/appdata/plex/download/porn_jav/"


def main():
    embed_model = None
    if "--model" in sys.argv:
        idx = sys.argv.index("--model")
        if idx + 1 < len(sys.argv):
            embed_model = sys.argv[idx + 1]
            if embed_model not in _EMBED_MODELS:
                print(f"Unknown model: {embed_model}")
                print(f"Available: {', '.join(_EMBED_MODELS)}")
                sys.exit(1)

    positive_suffix = None
    if "--positive" in sys.argv:
        idx = sys.argv.index("--positive")
        if idx + 1 < len(sys.argv):
            positive_suffix = sys.argv[idx + 1]

    db = ProcessedDB()

    # If --positive given, use the new DB helper
    if positive_suffix:
        video_infos = db.get_training_data(
            PROFILE_NAME, positive_suffix, fallback_video_dir=PLEX_DIR,
        )
        if not video_infos:
            print(f"No training data found for positive='{positive_suffix}'")
            sys.exit(1)
    else:
        # Legacy fallback: classify by folder path pattern
        rows = db._con.execute(
            "SELECT filename, start_time, output_path, source_path"
            " FROM processed WHERE profile = ?",
            (PROFILE_NAME,),
        ).fetchall()

        intense_by_video, soft_by_video = {}, {}
        source_by_fn = {}
        for fn, st, op, sp in rows:
            if sp:
                source_by_fn[fn] = sp
            if "/mp4_Intense/" in op or "_Intense/" in op:
                intense_by_video.setdefault(fn, set()).add(st)
            elif "/mp4_Soft/" in op or "_Soft/" in op:
                soft_by_video.setdefault(fn, set()).add(st)

        video_infos = []
        for fn in intense_by_video:
            # Try source_path from DB first, fall back to PLEX_DIR
            vpath = source_by_fn.get(fn) or os.path.join(PLEX_DIR, fn)
            if not os.path.exists(vpath):
                print(f"  skip (not found): {fn}")
                continue
            gt_intense = sorted(intense_by_video[fn])
            gt_soft = sorted(soft_by_video.get(fn, set()))
            video_infos.append((vpath, gt_intense, gt_soft))

    label = embed_model or "WAV2VEC2_BASE"
    print(f"Training {label} model on {len(video_infos)} videos...")
    model_path = default_model_path(PROFILE_NAME)
    result = train_classifier(
        video_infos, model_path=model_path, embed_model=embed_model,
    )
    if result is None:
        print("Training failed: no valid samples or missing class balance")
        sys.exit(1)
    print(f"Model saved to {model_path}")


if __name__ == "__main__":
    main()
