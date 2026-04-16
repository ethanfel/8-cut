import os
import subprocess
import tempfile

from .paths import _bin, _log

_yolo_model = None


def _get_yolo():
    """Lazy-load YOLOv8-nano. Returns None if ultralytics is not installed."""
    global _yolo_model
    if _yolo_model is None:
        try:
            from ultralytics import YOLO
            _yolo_model = YOLO("yolov8n.pt")
            _log("YOLO model loaded")
        except ImportError:
            _log("ultralytics not installed — tracking disabled")
            return None
        except Exception as e:
            _log(f"YOLO load failed: {e}")
            return None
    return _yolo_model


def extract_frame_cv(video_path: str, time: float):
    """Extract a single frame as a numpy array (BGR) via ffmpeg -> temp PNG -> cv2."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cmd = [_bin("ffmpeg"), "-y", "-ss", str(time), "-i", video_path,
               "-frames:v", "1", tmp]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            return None
        return cv2.imread(tmp)
    except Exception:
        return None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def detect_subject_center(
    video_path: str, time: float, target_cls: int | None, last_x: float, last_y: float,
) -> tuple[int | None, float, float] | None:
    """Detect objects at *time* and return (class_id, norm_x, norm_y) of the
    best match to (target_cls, last_x, last_y).  Returns None on failure."""
    model = _get_yolo()
    if model is None:
        return None
    frame = extract_frame_cv(video_path, time)
    if frame is None:
        return None
    results = model(frame, verbose=False)
    if not results or len(results[0].boxes) == 0:
        return None
    h, w = frame.shape[:2]
    dets = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        cls = int(box.cls[0])
        cx = (x1 + x2) / 2 / w
        cy = (y1 + y2) / 2 / h
        dets.append((cls, cx, cy))
    # Prefer same class, nearest to last known position.
    def score(d):
        cls_penalty = 0 if (target_cls is None or d[0] == target_cls) else 1.0
        dist = (d[1] - last_x) ** 2 + (d[2] - last_y) ** 2
        return cls_penalty + dist
    best = min(dets, key=score)
    return best


def track_centers_for_jobs(
    video_path: str, cursor: float, crop_center: float,
    starts: list[float],
) -> list[float]:
    """Run detection at the cursor (to identify the target) then at each start
    time.  Returns a list of horizontal crop centers (one per start)."""
    ref = detect_subject_center(video_path, cursor, None, crop_center, 0.5)
    if ref is None:
        _log("Tracking: no detection at cursor, using fixed center")
        return [crop_center] * len(starts)
    target_cls, last_x, last_y = ref
    _log(f"Tracking: target class={target_cls} at ({last_x:.2f}, {last_y:.2f})")
    centers = []
    for t in starts:
        det = detect_subject_center(video_path, t, target_cls, last_x, last_y)
        if det is not None:
            _, cx, cy = det
            _log(f"  t={t:.2f}s → center={cx:.3f}")
            centers.append(cx)
            last_x, last_y = cx, cy
        else:
            _log(f"  t={t:.2f}s → lost, reusing {last_x:.3f}")
            centers.append(last_x)
    return centers
