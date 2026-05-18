"""
Stream routes – start/stop stream + status.
"""

from __future__ import annotations
import asyncio
import base64
import concurrent.futures
import contextlib
import os
import time
import cv2
from functools import partial
from threading import Lock
from typing import Annotated
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.models.detection_model import StreamStartRequest, SuccessResponse, CompanionFramePayload
from app.services.stream_service import StreamService, stream_service
from app.services.ffmpeg_relay_service import (
    ffmpeg_relay_service,
    ffmpeg_relay_companion_service,
    ffmpeg_relay_extra2_service,
    ffmpeg_relay_extra3_service,
)
from app.services.ffmpeg_rtsp_decode import cuda_decode_enabled, ffmpeg_cuda_required
from app.core.config import settings
from app.ml.yolo_model import yolo_model

# Thread pool for thumbnail grabs — low concurrency limits parallel RTSP opens
_thumb_workers = max(1, min(int(getattr(settings, "THUMB_MAX_CONCURRENT", 2) or 2), 4))
_thumb_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_thumb_workers, thread_name_prefix="thumb"
)
_thumb_cache: dict[tuple[str, int, bool], tuple[dict, float]] = {}
_thumb_cache_lock = Lock()
_ffmpeg_env_lock = Lock()

_THUMB_FFMPEG_EXTRA = (
    "|fflags;discardcorrupt|flags;low_delay|err_detect;ignore_err|loglevel;quiet"
)

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


@router.post("/companion/start", response_model=SuccessResponse, status_code=202)
async def start_companion_stream(
    url: Annotated[str, Query(description="RTSP or video URL")],
):
    stream_service.start_companion(url)
    return SuccessResponse(success=True, message="Companion stream starting...")


@router.post("/companion/stop", response_model=SuccessResponse)
async def stop_companion_stream():
    stream_service.stop_companion()
    return SuccessResponse(success=True, message="Companion stream stopped")


@router.get("/extra/frame")
async def extra_frame(
    slot: Annotated[int, Query(description="Extra slot id", ge=2, le=3)],
):
    stream_service.mark_http_poll("extra", slot=slot)
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


@router.get("/runtime")
async def stream_runtime():
    """
    Runtime diagnostics for GPU-first tuning:
    - active YOLO backend/device
    - active model artifact (.pt/.engine)
    - active performance knobs from settings
    """
    d = _device_info()
    model_name = str(yolo_model.model_path or "")
    weights = yolo_model.weights_path
    loaded_artifact = str(weights) if weights else model_name
    return {
        "cuda_available": bool(d.get("cuda_available", False)),
        "cuda_device_name": d.get("device_name"),
        "yolo": {
            "loaded": bool(yolo_model.is_loaded),
            "model_name": model_name or None,
            "artifact_path": loaded_artifact or None,
            "artifact_ext": (loaded_artifact.rsplit(".", 1)[-1].lower() if "." in loaded_artifact else None),
            "runtime_backend": yolo_model.runtime_backend,
            "runtime_device": str(yolo_model.runtime_device),
            "runtime_half": bool(yolo_model.runtime_half),
            "backend_preference": str(getattr(settings, "YOLO_BACKEND", "auto")),
            "trt_auto_export": bool(getattr(settings, "YOLO_TRT_AUTO_EXPORT", False)),
            "trt_fp16": bool(getattr(settings, "YOLO_TRT_FP16", True)),
            "trt_workspace_gb": int(getattr(settings, "YOLO_TRT_WORKSPACE_GB", 4)),
        },
        "stream_tuning": {
            "yolo_device": str(getattr(settings, "YOLO_DEVICE", "auto")),
            "rtsp_hwaccel": bool(getattr(settings, "RTSP_HWACCEL", False)),
            "rtsp_hwaccel_auto": bool(getattr(settings, "RTSP_HWACCEL_AUTO", True)),
            "rtsp_cuda_decode_wanted": bool(StreamService._use_rtsp_cuda_decode()),
            "rtsp_cuda_device": int(getattr(settings, "RTSP_CUDA_DEVICE", 0)),
            "rtsp_cuda_extra_frames": int(getattr(settings, "RTSP_CUDA_EXTRA_FRAMES", 16)),
            "rtsp_d3d11_fallback": bool(getattr(settings, "RTSP_D3D11_FALLBACK", True)),
            "stream_cuda_resize": bool(getattr(settings, "STREAM_CUDA_RESIZE", False)),
            "h264_cuda_pipe_upload": bool(getattr(settings, "H264_CUDA_PIPE_UPLOAD", True)),
            "yolo_imgsz": int(getattr(settings, "YOLO_IMGSZ", 640)),
            # Effective runtime knobs (can diverge from .env after PATCH /settings).
            "inference_skip_frames": int(getattr(stream_service, "skip_frames", getattr(settings, "INFERENCE_SKIP_FRAMES", 0))),
            "stream_max_width": int(getattr(stream_service, "max_width", getattr(settings, "STREAM_MAX_WIDTH", 0))),
            "stream_jpeg_quality": int(getattr(stream_service, "jpeg_quality", getattr(settings, "STREAM_JPEG_QUALITY", 75))),
            "max_fps": int(getattr(stream_service, "max_fps", getattr(settings, "MAX_FPS", 30))),
            "companion_max_fps": int(getattr(settings, "COMPANION_MAX_FPS", 12)),
        },
    }


@router.get("/status")
async def stream_status():
    s = stream_service.stats
    return {
        "active": s.stream_active,
        "fps": s.fps,
        "frame_count": s.frame_count,
        "error": s.stream_error or None,
        "rtsp_decode": {
            "cuda_wanted": cuda_decode_enabled(),
            "cuda_required_no_cpu_fallback": ffmpeg_cuda_required(),
        },
    }


@router.get("/streams")
async def stream_streams():
    """Return which stream pipelines are currently running."""
    return stream_service.streams_status()


@router.get("/h264/status")
async def stream_h264_status(
    slot: Annotated[str, Query(description="H264 relay slot: primary|companion|extra2|extra3")] = "primary",
):
    """Status of H264 NVENC pipe (BGR burn-in + MPEG-TS over WebSocket) per slot."""
    s = (slot or "primary").strip().lower()
    if s in {"primary", "1", "main"}:
        return ffmpeg_relay_service.status()
    if s in {"companion", "2", "cam2"}:
        return ffmpeg_relay_companion_service.status()
    if s in {"extra2", "3", "cam3"}:
        return ffmpeg_relay_extra2_service.status()
    if s in {"extra3", "4", "cam4"}:
        return ffmpeg_relay_extra3_service.status()
    return {
        "running": False,
        "url_set": False,
        "bytes_sent": 0,
        "throughput_kbps": 0.0,
        "last_error": f"unknown h264 slot: {slot}",
    }


@router.get("/thumbnail")
async def stream_thumbnail(
    url: Annotated[str, Query(description="RTSP or video URL")],
    width: Annotated[int, Query(description="Max JPEG width (px); larger = sharper but heavier", ge=160, le=960)] = 320,
    fast: Annotated[
        bool,
        Query(description="Ít vòng grab sau flush — nhanh hơn; HEVC vẫn flush buffer trước khi lấy khung"),
    ] = False,
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


@contextlib.contextmanager
def _suppress_ffmpeg_stderr():
    """Hide OpenCV FFmpeg + libav HEVC noise during short thumbnail captures."""
    prev_av = os.environ.get("AV_LOG_LEVEL")
    os.environ["AV_LOG_LEVEL"] = "error"
    prev_cv_log = None
    try:
        log_mod = cv2.utils.logging
        prev_cv_log = log_mod.getLogLevel()
        log_mod.setLogLevel(log_mod.LOG_LEVEL_SILENT)
    except Exception:
        pass
    try:
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stderr(devnull):
                yield
    finally:
        if prev_av is None:
            os.environ.pop("AV_LOG_LEVEL", None)
        else:
            os.environ["AV_LOG_LEVEL"] = prev_av
        if prev_cv_log is not None:
            try:
                cv2.utils.logging.setLogLevel(prev_cv_log)
            except Exception:
                pass


def _valid_bgr_frame(frame: cv2.typing.MatLike | None) -> bool:
    if frame is None:
        return False
    try:
        if frame.size <= 0:
            return False
        h, w = frame.shape[:2]
        return w > 0 and h > 0
    except Exception:
        return False


def _thumb_use_cuda_decode() -> bool:
    if not bool(getattr(settings, "THUMB_CUDA_DECODE", True)):
        return False
    from app.services.ffmpeg_rtsp_decode import cuda_decode_enabled

    return cuda_decode_enabled()


def _read_thumbnail_frame(cap: cv2.VideoCapture, *, fast_decode: bool) -> cv2.typing.MatLike | None:
    """One frame for camera wall — CUDA FFmpeg pipe when cap is FFmpegRtspCapture."""
    from app.services.ffmpeg_rtsp_capture import FFmpegRtspCapture

    if isinstance(cap, FFmpegRtspCapture):
        flush_n = 1 if fast_decode else 2
        ok, fr = cap.read_fresh(flush_n)
        return fr if ok and _valid_bgr_frame(fr) else None

    try:
        flush_n = int(getattr(settings, "THUMB_FLUSH_FRAMES", 12) or 12)
    except (TypeError, ValueError):
        flush_n = 12
    flush_n = max(10, min(flush_n, 24)) if fast_decode else max(flush_n, 16)
    flush_n = max(6, min(flush_n, 28))
    if not fast_decode:
        flush_n = max(flush_n, 18)

    for _ in range(flush_n):
        cap.grab()

    max_attempts = 12 if fast_decode else 20
    frame: cv2.typing.MatLike | None = None
    for _ in range(max_attempts):
        if not cap.grab():
            break
        ret, f = cap.retrieve()
        if ret and _valid_bgr_frame(f):
            frame = f
            if fast_decode:
                break

    if frame is not None:
        return frame

    fallback_reads = 8 if fast_decode else 12
    min_idx = 3 if fast_decode else 5
    for i in range(fallback_reads):
        ret, f = cap.read()
        if ret and _valid_bgr_frame(f) and i >= min_idx:
            return f
    return None


def _grab_thumbnail(url: str, max_width: int = 320, *, fast_decode: bool = False) -> dict:
    """Synchronous: open stream, grab 1 frame, close immediately."""
    cache_key = (url, int(max_width), bool(fast_decode))
    ttl = float(getattr(settings, "THUMB_CACHE_TTL", 25.0) or 25.0)
    now = time.time()
    with _thumb_cache_lock:
        cached = _thumb_cache.get(cache_key)
        if cached and (now - cached[1]) < ttl:
            return cached[0]

    cap = None
    decode_mode = "cpu"
    mw = max(160, min(int(max_width), 960))
    try:
        is_rtsp = str(url).strip().lower().startswith("rtsp://")
        with _suppress_ffmpeg_stderr():
            with _ffmpeg_env_lock:
                if is_rtsp and _thumb_use_cuda_decode():
                    from app.services.ffmpeg_rtsp_capture import FFmpegRtspCapture

                    cap = FFmpegRtspCapture(
                        url,
                        label="thumb",
                        use_live_slot=False,
                        pipe_max_width=mw,
                    )
                    decode_mode = getattr(cap, "decode_backend", "") or "cuda"
                elif is_rtsp:
                    cap = stream_service._open_video_capture(
                        url,
                        is_rtsp=True,
                        ffmpeg_extra=_THUMB_FFMPEG_EXTRA,
                        prefer_ffmpeg=False,
                    )
                    decode_mode = "opencv"
                else:
                    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    decode_mode = "opencv"

            if cap is None or not cap.isOpened():
                err = getattr(cap, "_last_error", None) if cap is not None else None
                return {"ok": False, "frame": None, "error": err or "Cannot open stream"}

            from app.services.ffmpeg_rtsp_capture import FFmpegRtspCapture

            if not isinstance(cap, FFmpegRtspCapture):
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)
            frame = _read_thumbnail_frame(cap, fast_decode=fast_decode)

        if frame is None:
            return {"ok": False, "frame": None, "error": "No frame"}

        h, w = frame.shape[:2]
        if w <= 0:
            return {"ok": False, "frame": None, "error": "Bad frame size"}
        from app.utils.image_utils import resize_bgr_max_width

        thumb = resize_bgr_max_width(
            frame,
            mw,
            use_cuda=bool(getattr(settings, "STREAM_CUDA_RESIZE", False)),
        )

        quality = 82 if mw >= 480 else 76
        ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return {"ok": False, "frame": None, "error": "Encode failed"}
        b64 = base64.b64encode(buf.tobytes()).decode()
        result = {"ok": True, "frame": b64, "error": None, "decode": decode_mode}
        with _thumb_cache_lock:
            _thumb_cache[cache_key] = (result, time.time())
        return result

    except Exception as e:
        return {"ok": False, "frame": None, "error": str(e)}
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


@router.get("/frame")
async def stream_frame():
    """
    Return latest encoded frame + detections + stats.
    If no frame is available yet, returns an empty payload with current stats.
    """
    stream_service.mark_http_poll("primary")
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
    stream_service.mark_http_poll("companion")
    payload = stream_service.get_companion_latest()
    if payload is None:
        return CompanionFramePayload(frame=None, detections=[], fps=0.0, frame_count=0, stream_active=False)
    return payload
