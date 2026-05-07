"""
Stream routes – start/stop stream + status.
"""

from __future__ import annotations
import asyncio
import base64
import concurrent.futures
import cv2
from functools import partial
from typing import Annotated
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.models.detection_model import StreamStartRequest, SuccessResponse, CompanionFramePayload
from app.services.stream_service import stream_service

# Thread pool for thumbnail grabs (non-blocking)
_thumb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="thumb")

router = APIRouter(prefix="/api/v1/stream", tags=["stream"])


@router.post("/start", response_model=SuccessResponse, status_code=202)
async def start_stream(body: StreamStartRequest):
    """Start video capture in background; returns immediately (no blocking). YouTube resolve runs async."""
    stream_service.start(body.url, companion_url=body.companion_url)
    return SuccessResponse(success=True, message="Stream đang kết nối (YouTube có thể mất vài giây)...")


@router.post("/extra/start", response_model=SuccessResponse, status_code=202)
async def start_extra_stream(
    slot: Annotated[int, Query(description="Extra slot id (2=screen3, 3=screen4)", ge=2, le=3)],
    url: Annotated[str, Query(description="RTSP or video URL")],
):
    stream_service.start_extra(slot, url)
    return SuccessResponse(success=True, message=f"Extra stream {slot} starting...")


@router.post("/extra/stop", response_model=SuccessResponse)
async def stop_extra_stream(
    slot: Annotated[int, Query(description="Extra slot id", ge=2, le=3)],
):
    stream_service.stop_extra(slot)
    return SuccessResponse(success=True, message=f"Extra stream {slot} stopped")


@router.get("/extra/frame")
async def extra_frame(
    slot: Annotated[int, Query(description="Extra slot id", ge=2, le=3)],
):
    payload = stream_service.get_extra_latest(slot)
    if payload is None:
        return {"slot": slot, "frame": None, "detections": [], "fps": 0.0, "stream_active": False}
    return payload


@router.post("/stop", response_model=SuccessResponse)
async def stop_stream():
    """Stop the active stream."""
    stream_service.stop()
    return SuccessResponse(success=True, message="Stream stopped")


def _device_info():
    """Return CUDA/CPU device info for display in UI."""
    try:
        import torch
        cuda = torch.cuda.is_available()
        name = torch.cuda.get_device_name(0) if cuda else None
        return {"cuda_available": cuda, "device_name": name}
    except Exception:
        return {"cuda_available": False, "device_name": None}


@router.get("/device")
async def stream_device():
    """Return whether backend is using GPU (CUDA) or CPU. For UI status pill."""
    return _device_info()


@router.get("/status")
async def stream_status():
    s = stream_service.stats
    return {
        "active": s.stream_active,
        "fps": s.fps,
        "frame_count": s.frame_count,
        "error": s.stream_error or None,
    }


@router.get("/thumbnail")
async def stream_thumbnail(
    url: Annotated[str, Query(description="RTSP or video URL")],
    width: Annotated[int, Query(description="Max JPEG width (px); larger = sharper but heavier", ge=160, le=960)] = 320,
    fast: Annotated[bool, Query(description="Bớt vòng discard HEVC — nhanh hơn nhưng vài camera có artefact")] = False,
):
    """
    Grab a single frame from any RTSP/video URL and return as base64 JPEG.
    Used by the camera wall to show live previews without starting the main stream.
    Runs in a thread pool so it doesn't block the event loop.
    """
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _thumb_executor, partial(_grab_thumbnail, url, width, fast_decode=fast)
    )
    return JSONResponse(result)


def _grab_thumbnail(url: str, max_width: int = 320, *, fast_decode: bool = False) -> dict:
    """Synchronous: open stream, grab 1 frame, close immediately."""
    try:
        import os
        os.environ.setdefault(
            "OPENCV_FFMPEG_CAPTURE_OPTIONS",
            "rtsp_transport;tcp|buffer_size;4096000|max_delay;500000|stimeout;5000000|fflags;nobuffer|flags;low_delay"
        )
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)

        if not cap.isOpened():
            return {"ok": False, "frame": None, "error": "Cannot open stream"}

        # HEVC: bỏ vài khung đầu để decoder ổn; fast_decode bớt vòng chờ → giảm trễ khi nhiều preview
        frame: cv2.typing.MatLike | None = None
        max_iter, take_from = (10, 5) if fast_decode else (15, 8)
        for i in range(max_iter):
            ret, f = cap.read()
            if ret and f is not None and i >= take_from:
                frame = f
                break

        cap.release()

        if frame is None:
            return {"ok": False, "frame": None, "error": "No frame"}

        mw = max(160, min(int(max_width), 960))
        h, w = frame.shape[:2]
        if w <= 0:
            return {"ok": False, "frame": None, "error": "Bad frame size"}
        scale = min(1.0, mw / float(w))
        thumb_w = max(1, int(w * scale))
        thumb_h = max(1, int(h * scale))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        thumb = cv2.resize(frame, (thumb_w, thumb_h), interpolation=interp)

        quality = 82 if thumb_w >= 480 else 76
        _, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, quality])
        b64 = base64.b64encode(buf.tobytes()).decode()
        return {"ok": True, "frame": b64, "error": None}

    except Exception as e:
        return {"ok": False, "frame": None, "error": str(e)}


@router.get("/frame")
async def stream_frame():
    """
    Return latest encoded frame + detections + stats.
    If no frame is available yet, returns an empty payload with current stats.
    """
    payload = stream_service.get_latest()
    if payload is None:
        # Keep response shape stable for frontend
        s = stream_service.stats
        return {
            "frame": None,
            "detections": [],
            "stats": s.model_dump(),
        }
    return payload


@router.get("/companion/frame", response_model=CompanionFramePayload)
async def stream_companion_frame():
    """
    Return latest companion (second RTSP) frame + detections.
    If companion is not enabled or not ready yet, frame is null.
    """
    payload = stream_service.get_companion_latest()
    if payload is None:
        return CompanionFramePayload(frame=None, detections=[], fps=0.0, frame_count=0, stream_active=False)
    return payload
