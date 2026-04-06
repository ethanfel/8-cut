"""SAM2 mask generation script.

Usage:
    python tools/sam_masks.py --input video.mp4 --output masks_dir/

Outputs one binary PNG per frame: frame_0000.png, frame_0001.png, …
Uses center of first frame as positive point prompt, propagates across all frames.
Requires: torch, segment-anything-2, opencv-python
"""
import argparse
import os
import sys
import tempfile

import cv2
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}", flush=True)

    # Extract frames to temp directory (SAM2 video predictor needs image files)
    with tempfile.TemporaryDirectory() as frame_dir:
        cap = cv2.VideoCapture(args.input)
        if not cap.isOpened():
            print(f"ERROR: cannot open {args.input}", file=sys.stderr)
            sys.exit(1)

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            cv2.imwrite(os.path.join(frame_dir, f"{idx:04d}.jpg"), frame)
            idx += 1
        cap.release()

        print(f"Extracted {idx} frames", flush=True)

        # SAM2: use from_pretrained (SAM2.1+ / HuggingFace integration)
        from sam2.sam2_video_predictor import SAM2VideoPredictor

        predictor = SAM2VideoPredictor.from_pretrained(
            "facebook/sam2-hiera-large"
        ).to(device)

        with torch.inference_mode():
            state = predictor.init_state(video_path=frame_dir)

            # Center of first frame as positive point prompt
            cx, cy = width // 2, height // 2
            _, _, _ = predictor.add_new_points_or_box(
                inference_state=state,
                frame_idx=0,
                obj_id=1,
                points=np.array([[cx, cy]], dtype=np.float32),
                labels=np.array([1], dtype=np.int32),
            )

            for frame_idx, obj_ids, out_mask_logits in predictor.propagate_in_video(state):
                # out_mask_logits: (N_objects, 1, H, W) — threshold logits at 0
                mask = (out_mask_logits[0].squeeze().cpu().numpy() > 0.0).astype(np.uint8) * 255
                out_path = os.path.join(args.output, f"frame_{frame_idx:04d}.png")
                cv2.imwrite(out_path, mask)
                print(f"frame {frame_idx + 1}/{total}", flush=True)

    print("done", flush=True)


if __name__ == "__main__":
    main()
