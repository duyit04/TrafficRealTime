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

    # Companion RTSP (second panel) — lightweight inference FPS cap
    COMPANION_MAX_FPS: int = 12

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


settings = Settings()

settings.MODELS_DIR.mkdir(parents=True, exist_ok=True)
settings.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
