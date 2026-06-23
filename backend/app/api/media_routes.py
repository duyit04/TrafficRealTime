"""
Media API: image detection + video file upload/stream.

POST /api/v1/media/detect-image  – upload image → detect → return annotated image + detections
POST /api/v1/media/start-video   – upload video → save to uploads → start H264 stream
POST /api/v1/media/stop-video    – stop video processing
GET  /api/v1/media/status        – current source mode + running flag
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.config import settings
from app.core.logger import logger
from app.ml.yolo_model import yolo_model
router = APIRouter(prefix="/api/v1/media", tags=["media"])

_ALLOWED_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".ts", ".m4v"}


def _svc():
    from app.services.stream_service import stream_service
    return stream_service


@router.post("/detect-image")
async def detect_image(file: UploadFile = File(...)):
    """Upload an image, run YOLO detection, return annotated JPEG (base64) + detection list."""
    if not yolo_model.is_loaded:
        raise HTTPException(400, "Chưa có model nào được load")

    raw = await file.read()
    arr = np.frombuffer(raw, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Không thể đọc ảnh — kiểm tra định dạng file")

    svc = _svc()
    dets = yolo_model.predict(frame, conf=svc.conf_threshold)

    api_dets = [
        {
            "x1": d.x1, "y1": d.y1, "x2": d.x2, "y2": d.y2,
            "class_name": d.class_name,
            "confidence": d.confidence,
            "track_id": None,
        }
        for d in dets
    ]
    # Frontend draws boxes — return raw frame to avoid double annotation + scale mismatch.
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    img_b64 = base64.b64encode(buf.tobytes()).decode()

    return {
        "success": True,
        "image": img_b64,
        "width": int(frame.shape[1]),
        "height": int(frame.shape[0]),
        "detections": api_dets,
        "count": len(api_dets),
    }


@router.post("/start-video")
async def start_video(file: UploadFile = File(...)):
    """Upload a local video file → save to uploads → start detection+tracking stream."""
    if not yolo_model.is_loaded:
        raise HTTPException(400, "Chưa có model nào được load")

    fname = Path(file.filename or "video.mp4").name
    suffix = Path(fname).suffix.lower()
    if suffix not in _ALLOWED_VIDEO_EXT:
        suffix = ".mp4"

    tmp_path = settings.UPLOADS_DIR / f"vid_{int(time.time() * 1000)}{suffix}"

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(400, "File rỗng")

    tmp_path.write_bytes(raw)
    logger.info("media: saved video upload → %s (%d bytes)", tmp_path.name, len(raw))

    _svc().start_video_file(str(tmp_path))

    return {"success": True, "filename": fname, "size": len(raw)}


@router.post("/stop-video")
async def stop_video():
    """Stop the current video file stream."""
    _svc().stop()
    return {"success": True}


@router.get("/status")
async def media_status():
    """Return current source mode and running state."""
    svc = _svc()
    return {
        "source_mode": getattr(svc, "_source_mode", "rtsp"),
        "running": bool(svc._running),
        "stream_active": bool(svc._stats.stream_active),
    }
