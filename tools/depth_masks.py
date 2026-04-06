"""Depth Anything V2 mask generation script.

Usage:
    python tools/depth_masks.py --input video.mp4 --output masks_dir/

Outputs one binary PNG per frame: frame_0000.png, frame_0001.png, …
Foreground = white (255), background = black (0), via Otsu threshold on depth map.
Requires: torch, transformers, opencv-python, Pillow
"""
import argparse
import os
import sys

import cv2
import numpy as np
from PIL import Image
from transformers import pipeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}", flush=True)

    pipe = pipeline(
        "depth-estimation",
        model="depth-anything/Depth-Anything-V2-Large-hf",
        device=device,
    )

    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        print(f"ERROR: cannot open {args.input}", file=sys.stderr)
        sys.exit(1)

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        result = pipe(pil_img)
        depth = np.array(result["depth"])  # float32 array

        # Normalise to 0–255
        d_min, d_max = depth.min(), depth.max()
        if d_max > d_min:
            depth_u8 = ((depth - d_min) / (d_max - d_min) * 255).astype(np.uint8)
        else:
            depth_u8 = np.zeros_like(depth, dtype=np.uint8)

        # Otsu threshold: closer objects (higher depth value) = foreground
        _, mask = cv2.threshold(depth_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        out_path = os.path.join(args.output, f"frame_{idx:04d}.png")
        cv2.imwrite(out_path, mask)

        idx += 1
        print(f"frame {idx}/{total}", flush=True)

    cap.release()
    print("done", flush=True)


if __name__ == "__main__":
    main()
