"""
Pydantic data models for the Traffic Monitor API.
Used for request/response validation and WebSocket payloads.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


# ── Detection Models ──────────────────────────────────────────────────────────

class BoundingBox(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int


class Detection(BaseModel):
    bbox: BoundingBox
    class_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    track_id: Optional[int] = None


class CongestionInfo(BaseModel):
    """Congestion state sent to frontend."""
    is_congested: bool = False
    vehicle_count: int = 0
    threshold: int = 10
    duration_seconds: float = 0.0
    stable_duration: float = 5.0
    message: str = ""
    level: str = "normal"  # normal | warning | critical


class VehicleStats(BaseModel):
    total: int = 0
    classes: dict[str, int] = {}
    fps: float = 0.0
    fps_capture: float = 0.0        # frames read from RTSP per second
    fps_inference: float = 0.0      # YOLO inference frames per second
    fps_sent: float = 0.0           # frames broadcast to clients per second
    avg_inference_ms: float = 0.0   # sliding-window avg of last 10 inference times (ms)
    frame_count: int = 0
    stream_active: bool = False
    model_loaded: bool = False
    model_name: str = ""
    roi_active: bool = False
    roi_count: int = 0              # vehicles currently INSIDE the ROI (live, this frame)
    roi_total: int = 0              # cumulative vehicles that entered ROI (unique track IDs)
    roi_classes: dict[str, int] = {}  # per-class ROI entry count
    conf_threshold: float = 0.35
    line_position: float = 0.55
    stream_error: str = ""
    congestion: CongestionInfo = Field(default_factory=CongestionInfo)


# ── WebSocket Payload ─────────────────────────────────────────────────────────

class FramePayload(BaseModel):
    """Payload sent over WebSocket for each processed frame."""
    frame: str              # base64-encoded JPEG
    detections: list[Detection] = []
    stats: VehicleStats


# ── Companion (second stream) payload ─────────────────────────────────────────

class CompanionFramePayload(BaseModel):
    """
    Second RTSP live frame (for dual-view UI).
    Keep it lightweight: no counters, no congestion; only frame + detections + basic runtime flags.
    """
    frame: str | None = None  # base64-encoded JPEG
    detections: list[Detection] = []
    fps: float = 0.0
    frame_count: int = 0
    stream_active: bool = False


# ── Request Models ────────────────────────────────────────────────────────────

class StreamStartRequest(BaseModel):
    url: str = Field(..., description="RTSP URL or local video file path")
    companion_url: Optional[str] = Field(
        None,
        description="Optional second RTSP (other approach at same intersection). Enables dual-lane TLC density.",
    )


class RoiRequest(BaseModel):
    points: list[list[float]] = Field(
        ..., description="List of [x, y] pixel coordinates"
    )
    active: bool = True


class SettingsUpdate(BaseModel):
    conf_threshold: Optional[float] = Field(None, ge=0.1, le=0.95)
    line_position: Optional[float] = Field(None, ge=0.05, le=0.95)
    max_fps: Optional[int] = Field(None, ge=1, le=60)
    tracker_type: Optional[str] = Field(None, description="sort | deepsort | bytetrack")
    congestion_threshold: Optional[int] = Field(None, ge=1, le=100)
    congestion_duration: Optional[float] = Field(None, ge=1.0, le=120.0)
    jpeg_quality: Optional[int] = Field(None, ge=30, le=95)
    max_width: Optional[int] = Field(None, ge=0, le=1920)
    skip_frames: Optional[int] = Field(None, ge=0, le=10)


class ModelLoadRequest(BaseModel):
    name: str


class ModelExportEngineRequest(BaseModel):
    name: str = Field(..., description="Model name/path to .pt file under models storage")
    fp16: Optional[bool] = Field(None, description="Override FP16 export (default from settings)")
    workspace_gb: Optional[int] = Field(None, ge=1, le=64, description="TensorRT workspace size in GB")
    imgsz: Optional[int] = Field(None, ge=320, le=1280, description="Export input size")
    load_after_export: bool = Field(True, description="Load exported .engine as active model")


# ── Response Models ───────────────────────────────────────────────────────────

class ModelInfo(BaseModel):
    name: str
    size_mb: float
    active: bool


class SuccessResponse(BaseModel):
    success: bool
    message: str = ""


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
