"""
Model routes – upload, list, load, delete YOLO .pt/.engine files.
"""

from __future__ import annotations
import datetime
import math
import threading
import time
from fastapi import APIRouter, UploadFile, File, HTTPException
from pathlib import Path

from app.core.config import settings
from app.core.logger import logger
from app.models.detection_model import (
    ModelInfo, ModelLoadRequest, ModelExportEngineRequest, SuccessResponse, ErrorResponse
)
from app.services.model_service import model_service

router = APIRouter(prefix="/api/v1/models", tags=["models"])
_engine_job_lock = threading.Lock()
_engine_job: dict = {
    "running": False,
    "done": False,
    "ok": False,
    "error": None,
    "model": None,
    "engine": None,
    "started_at": None,
    "ended_at": None,
    # 0–100: ước lượng trong lúc Ultralytics/TensorRT build (blocking); kết thúc ép 100
    "progress": 0,
    "progress_message": "",
}


@router.get("", response_model=list[ModelInfo])
async def list_models():
    """Return all available .pt/.engine models."""
    return model_service.list_models()


@router.post("/upload", response_model=SuccessResponse)
async def upload_model(file: UploadFile = File(...)):
    """Upload a YOLOv8 .pt/.engine file; store in models_storage and load as active model."""
    if not file.filename or (not file.filename.endswith(".pt") and not file.filename.endswith(".engine")):
        raise HTTPException(status_code=400, detail="Only .pt or .engine files are accepted")

    # Store in dedicated models folder (models_storage) per architecture spec
    save_path = settings.MODELS_DIR / file.filename
    content = await file.read()
    save_path.write_bytes(content)

    try:
        model_service.load(save_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return SuccessResponse(success=True, message=f"Model '{file.filename}' loaded ✓")


@router.post("/load", response_model=SuccessResponse)
async def load_model(body: ModelLoadRequest):
    """Load a previously uploaded model by name. Use 'default' for YOLOv8n."""
    try:
        if body.name == "default":
            # Load pretrained YOLOv8n (auto-downloads from ultralytics hub)
            from app.ml.yolo_model import yolo_model
            yolo_model.load_pretrained("yolov8n.pt")
            return SuccessResponse(success=True, message="Loaded default YOLOv8n")
        path = model_service._resolve(body.name)
        model_service.load(path)
        return SuccessResponse(success=True, message=f"Loaded '{body.name}'")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Model not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/export-engine", response_model=SuccessResponse)
async def export_engine(body: ModelExportEngineRequest):
    """
    Start background export of .pt -> TensorRT .engine.
    Returns immediately to avoid request timeout on long exports.
    """
    with _engine_job_lock:
        if _engine_job.get("running"):
            raise HTTPException(status_code=409, detail="An engine export is already running")
        now = time.time()
        _engine_job.update(
            {
                "running": True,
                "done": False,
                "ok": False,
                "error": None,
                "model": body.name,
                "engine": None,
                "started_at": now,
                "ended_at": None,
                "progress": 2,
                "progress_message": "Đang chuẩn bị export TensorRT…",
            }
        )

    stop_progress = threading.Event()

    def _progress_loop():
        """Tiến độ ước lượng (export là một call blocking — không có hook % thực)."""
        started = time.time()
        while not stop_progress.wait(timeout=1.25):
            with _engine_job_lock:
                if not _engine_job.get("running"):
                    return
                elapsed = time.time() - started
                # Tiệm cận ~93% sau ~2–3 phút; không đạt 100 cho tới khi export xong
                p = 3 + 93 * (1 - math.exp(-elapsed / 55.0))
                _engine_job["progress"] = min(96, int(round(p)))
                _engine_job["progress_message"] = "Đang build TensorRT engine (có thể vài phút)…"

    def _job():
        # Backup existing .engine if present (prevents silent overwrite)
        backed_up: str | None = None
        try:
            src = model_service._resolve(body.name)
            expected_engine = src.with_suffix(".engine")
            if expected_engine.exists():
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = expected_engine.parent / f"{expected_engine.stem}_backup_{ts}.engine"
                expected_engine.rename(backup)
                backed_up = backup.name
                logger.info("export_engine: backed up %s → %s", expected_engine.name, backup.name)
        except Exception as _be:
            logger.warning("export_engine: backup check failed: %s", _be)

        prog_thread = threading.Thread(target=_progress_loop, daemon=True)
        prog_thread.start()
        try:
            out = model_service.export_engine(
                body.name,
                fp16=body.fp16,
                workspace_gb=body.workspace_gb,
                imgsz=body.imgsz,
                load_after_export=body.load_after_export,
            )
            stop_progress.set()
            prog_thread.join(timeout=2)
            done_msg = "Export hoàn thành"
            if backed_up:
                done_msg += f" (file cũ đã backup: {backed_up})"
            with _engine_job_lock:
                _engine_job.update(
                    {
                        "running": False,
                        "done": True,
                        "ok": True,
                        "error": None,
                        "engine": str(out),
                        "ended_at": time.time(),
                        "progress": 100,
                        "progress_message": done_msg,
                    }
                )
        except Exception as exc:
            stop_progress.set()
            prog_thread.join(timeout=2)
            with _engine_job_lock:
                _engine_job.update(
                    {
                        "running": False,
                        "done": True,
                        "ok": False,
                        "error": str(exc),
                        "ended_at": time.time(),
                        "progress_message": "Export thất bại",
                    }
                )

    threading.Thread(target=_job, daemon=True).start()
    return SuccessResponse(success=True, message="Engine export started in background")


@router.get("/export-engine/status")
async def export_engine_status():
    """Get status of latest TensorRT export job."""
    with _engine_job_lock:
        return dict(_engine_job)


@router.delete("/{name}", response_model=SuccessResponse)
async def delete_model(name: str):
    """Delete a model file from storage."""
    try:
        model_service.delete(name)
        return SuccessResponse(success=True, message=f"Deleted '{name}'")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Model not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
