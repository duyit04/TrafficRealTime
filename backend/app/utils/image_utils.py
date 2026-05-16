"""Image utility functions."""

import cv2
import base64
import numpy as np


def encode_frame_base64(frame: np.ndarray, quality: int = 80) -> str:
    """Encode OpenCV BGR frame as base64 JPEG string."""
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode()


def resize_frame(frame: np.ndarray, max_width: int = 1280) -> np.ndarray:
    """Resize frame to max_width while maintaining aspect ratio."""
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame
    scale = max_width / w
    return cv2.resize(frame, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)


def resize_bgr_max_width(frame: np.ndarray, max_width: int, *, use_cuda: bool = False) -> np.ndarray:
    """
    Downscale BGR frame to max_width (aspect preserved). INTER_AREA on CPU.
    If use_cuda and OpenCV is built with CUDA, uses cv2.cuda.resize (often absent on pip wheels).
    """
    if max_width <= 0 or frame is None or frame.ndim < 2:
        return frame
    h, w = int(frame.shape[0]), int(frame.shape[1])
    if w <= max_width:
        return frame
    new_h = max(1, int(h * (max_width / float(w))))
    if not use_cuda:
        return cv2.resize(frame, (max_width, new_h), interpolation=cv2.INTER_AREA)
    try:
        if not hasattr(cv2, "cuda") or int(cv2.cuda.getCudaEnabledDeviceCount()) < 1:
            return cv2.resize(frame, (max_width, new_h), interpolation=cv2.INTER_AREA)
        g = cv2.cuda_GpuMat()
        g.upload(frame)
        out = cv2.cuda.resize(g, (max_width, new_h), interpolation=cv2.INTER_LINEAR)
        return out.download()
    except Exception:
        return cv2.resize(frame, (max_width, new_h), interpolation=cv2.INTER_AREA)
