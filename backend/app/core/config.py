"""
Traffic Monitor FastAPI Backend
Core configuration using Pydantic Settings.
"""

from pydantic_settings import BaseSettings
from pathlib import Path
from typing import List


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Traffic Monitor API"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:5000",
    ]

    # Storage
    MODELS_DIR: Path = Path("models_storage")
    UPLOADS_DIR: Path = Path("uploads")

    # Detection defaults
    CONF_THRESHOLD: float = 0.35
    LINE_POSITION: float = 0.55
    MAX_FPS: int = 30
    TRACKER_TYPE: str = "bytetrack"  # bytetrack | botsort

    # YOLO: auto uses GPU if PyTorch is built with CUDA and a GPU is visible; otherwise CPU.
    # Set to "cpu" to force CPU, or "cuda" / "0" to prefer GPU (logs a warning and falls back if CUDA missing).
    YOLO_DEVICE: str = "auto"
    # Runtime backend preference: auto | torch | tensorrt.
    # - auto: use TensorRT engine if available on CUDA, otherwise PyTorch.
    # - torch: force PyTorch runtime.
    # - tensorrt: prefer TensorRT; auto-fallback to PyTorch on failure.
    YOLO_BACKEND: str = "auto"
    # If True and TensorRT backend is preferred, export .pt -> .engine when sidecar engine is missing.
    YOLO_TRT_AUTO_EXPORT: bool = False
    # Prefer FP16 TensorRT engine export.
    YOLO_TRT_FP16: bool = True
    # TensorRT export workspace size in GB (used by Ultralytics export where supported).
    YOLO_TRT_WORKSPACE_GB: int = 4

    # Companion RTSP (second panel) — lightweight inference FPS cap
    COMPANION_MAX_FPS: int = 12
    # Whether to run YOLO inference on companion stream
    COMPANION_DETECT_ENABLED: bool = True
    # Optional lower imgsz for companion stream (0 = use YOLO_IMGSZ)
    COMPANION_YOLO_IMGSZ: int = 0
    # Optional skip-frames override for companion stream (None = use INFERENCE_SKIP_FRAMES)
    COMPANION_SKIP_FRAMES: int | None = None

    # Extra live streams (screens 3/4) — FPS cap
    EXTRA_MAX_FPS: int = 12

    # ── Stream output quality ──────────────────────────────────────────────────
    # JPEG quality for encoded frames sent to frontend (30–95). Lower = smaller payload = higher FPS.
    STREAM_JPEG_QUALITY: int = 75
    # Resize frame width before JPEG encode (0 = no resize). E.g. 960 halves a 1920px stream.
    STREAM_MAX_WIDTH: int = 0
    # Skip N frames between YOLO inferences (0 = run every frame).
    # E.g. skip_frames=2 → inference on frame 1, skip 2&3, inference on 4, ...
    # Skipped frames reuse last detections but still get encoded and sent → smoother video.
    INFERENCE_SKIP_FRAMES: int = 0
    # RTSP buffer flush: grab this many extra frames before retrieve() to get freshest frame.
    RTSP_FLUSH_FRAMES: int = 2
    # Try FFmpeg hardware decode on CUDA for RTSP (reduces CPU decode load on supported setups).
    RTSP_HWACCEL: bool = False

    # ── YOLO inference size ────────────────────────────────────────────────────
    # Input image size for YOLO inference. Valid values: 320, 416, 480, 640.
    # Smaller = faster but less accurate. Invalid values fall back to 640.
    YOLO_IMGSZ: int = 640

    # Auto-load model
    DEFAULT_MODEL: str = "best.pt"

    # Congestion detection
    CONGESTION_VEHICLE_THRESHOLD: int = 10
    CONGESTION_STABLE_DURATION: float = 5.0

    # Traffic light controller (ATCS-style)
    TLC_STOP_SPEED_PX_S: float = 8.0      # below this -> treat as stopped (px/s, scale with resolution)
    TLC_MIN_STOPPED_FRAMES: int = 5       # consecutive low-motion frames to count as waiting
    TLC_GAP_CLEAR_S: float = 3.0          # green ends early if ROW queue empty this long
    TLC_EXTENSION_STEP_S: float = 4.0     # add to green when extending
    TLC_NEAR_END_EXTEND_S: float = 4.0    # extend when remaining green below this
    TLC_EXTENSION_MIN_APPROACHING: int = 2
    TLC_YELLOW_SECONDS: float = 3.0
    TLC_ALL_RED_SECONDS: float = 1.5
    TLC_MIN_GREEN: float = 10.0
    TLC_MAX_GREEN: float = 70.0
    TLC_MAX_RED_WAIT: float = 90.0        # opposite max track wait -> force end green
    TLC_WAIT_PENALTY_MAX: float = 15.0    # reduces planned green when opposite waits (scale to MAX_RED)
    # If True: gap-out only when the red approach still has stopped queue (classic ATCS fairness)
    TLC_GAP_REQUIRE_OPP_QUEUE: bool = False
    # If True: gap-out only when the green ROW has zero *moving* vehicles (approaching count),
    # not only zero stopped queue — avoids yellow while cars still flow through the approach.
    TLC_GAP_REQUIRE_ZERO_APPROACHING: bool = True
    # Horizontal band around counting line (fraction of frame height, each side) for "in intersection"
    TLC_INTERSECTION_HALF_BAND_FRAC: float = 0.035
    # If True: gap-out blocked while any vehicle centroid lies in that band (see ATCS patchable frac)
    TLC_GAP_REQUIRE_INTERSECTION_CLEAR: bool = True
    # Fuzzy green-time boost from approaching vehicles (seconds ≈ coeff * count, capped)
    TLC_FUZZY_APPROACH_BOOST: float = 0.55
    TLC_FUZZY_APPROACH_BOOST_MAX: float = 14.0
    # Suggested green-time from stopped queue in ROI (seconds per stopped vehicle)
    TLC_STOPPED_GREEN_COEFF: float = 2.5
    # Joint two-ROI advice: baseline when queues empty (replaces TLC_MIN_GREEN for the formula base)
    TLC_ADVICE_DEFAULT_SECONDS: float = 30.0
    # Subtract from coupled green G per stopped vehicle on the approach holding ROW (density on green side ⇒ red block shrinks).
    TLC_ADVICE_CROSS_QUEUE_COEFF: float = 1.5
    # After all-red: if the scheduled green phase has no queue/approaching but the other does, serve the other first
    TLC_ACTUATED_PREFER_DEMAND_PHASE: bool = True
    # Fraction of max_red_wait beyond which UI shows "priority" hint before hard force
    TLC_PRIORITY_WAIT_FRACTION: float = 0.67

    # Inverse traffic-light inference (behavior → phase), no camera on signal head
    TLC_INFER_ENABLED: bool = True
    TLC_INFER_CROSSING_WINDOW_S: float = 3.0
    TLC_INFER_STOPPED_PX_S: float = 8.0
    TLC_INFER_MOVING_PX_S: float = 15.0
    TLC_INFER_GREEN_CROSS_PER_S: float = 0.12
    TLC_INFER_GREEN_FRONT_PX_S: float = 12.0
    TLC_INFER_MOVING_RATIO_GREEN: float = 0.45
    TLC_INFER_RED_MAX_AVG_PX_S: float = 6.0
    TLC_INFER_RED_CROSS_MAX: float = 0.04
    TLC_INFER_YELLOW_MIN_CROSS: float = 0.02
    TLC_INFER_MIN_PHASE_S: float = 4.0
    TLC_INFER_HISTORY_LEN: int = 8

    # Bù thời gian xanh từ suy luận hai hướng (ROI + hành vi), trước khi penal đợi đối diện
    TLC_INFER_BALANCE_ENABLED: bool = False
    TLC_INFER_BALANCE_MAX_TRIM: float = 12.0
    TLC_INFER_BALANCE_MIN_CONF: float = 0.52
    TLC_INFER_BALANCE_QUEUE_OPP_FLOOR: float = 3.0
    TLC_INFER_BALANCE_QUEUE_SLACK: float = 2.0

    # ── Crop signal head on main RTSP + optional second lane stream ─────────────
    # TLC_SIGNAL_BBOX: fractions y1,y2,x1,x2 on main frame — red pixel ratio gates "dem khi den do".
    # Empty string = không dùng pixel đỏ (gợi ý mật độ luôn bật khi có ROI).
    TLC_SIGNAL_BBOX: str = ""
    TLC_VISUAL_RED_ON_RATIO: float = 0.065
    TLC_VISUAL_RED_OFF_RATIO: float = 0.028
    # Bbox đếm xe trên luồng phụ companion (POST /stream/start companion_url).
    TLC_COMPANION_VEHICLE_BBOX: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


_VALID_IMGSZ = {320, 416, 480, 640}


def _make_settings() -> "Settings":
    s = Settings()
    if s.YOLO_IMGSZ not in _VALID_IMGSZ:
        import logging
        logging.getLogger(__name__).warning(
            "YOLO_IMGSZ=%d is invalid (valid: %s). Falling back to 640.",
            s.YOLO_IMGSZ,
            sorted(_VALID_IMGSZ),
        )
        # Pydantic settings are immutable after creation; use object.__setattr__
        object.__setattr__(s, "YOLO_IMGSZ", 640)
    return s


settings = _make_settings()

settings.MODELS_DIR.mkdir(parents=True, exist_ok=True)
settings.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
