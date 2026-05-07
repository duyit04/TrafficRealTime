"""
ML Layer – YOLOv8 Model Wrapper
Encapsulates all direct interactions with the ultralytics YOLO library.
The service layer never imports ultralytics directly.

Supports both:
  - predict(): detection only (no tracking)
  - track(): detection + built-in tracking (ByteTrack / BoT-SORT)
"""

from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass
from typing import List
import threading

import numpy as np

from app.core.config import settings
from app.core.logger import logger


def _resolve_yolo_device():
    """
    Pick Ultralytics device + half precision from settings and torch capabilities.
    Returns (device, use_half, label for logs).
    """
    import torch

    mode = (settings.YOLO_DEVICE or "auto").strip().lower()
    cuda_ok = torch.cuda.is_available()
    ver = getattr(torch, "__version__", "?")

    def with_name(dev, half: bool, label: str):
        return dev, half, label

    if mode == "auto":
        if cuda_ok:
            name = torch.cuda.get_device_name(0)
            return with_name(0, True, f"cuda:0 ({name})")
        hint = ""
        if "+cpu" in ver:
            hint = (
                " Install a CUDA build matching your Python version (see "
                "https://pytorch.org/get-started/locally/ ). "
                "Example (Python 3.14 / Windows): --index-url https://download.pytorch.org/whl/cu126"
            )
        logger.warning(
            "YOLOModel: CUDA not available (torch %s). Using CPU.%s", ver, hint
        )
        return with_name("cpu", False, "cpu")

    if mode == "cpu":
        return with_name("cpu", False, "cpu")

    if mode in ("cuda", "gpu", "cuda:0", "0"):
        if cuda_ok:
            name = torch.cuda.get_device_name(0)
            return with_name(0, True, f"cuda:0 ({name})")
        logger.warning(
            "YOLO_DEVICE requests GPU but CUDA is unavailable (torch %s). Using CPU.", ver
        )
        return with_name("cpu", False, "cpu")

    if cuda_ok:
        return with_name(mode, True, mode)
    logger.warning("YOLO_DEVICE=%s but CUDA unavailable; using CPU", mode)
    return with_name("cpu", False, "cpu")


@dataclass
class RawDetection:
    """Raw output from YOLOv8 before any business logic."""
    x1: int
    y1: int
    x2: int
    y2: int
    class_id: int
    class_name: str
    confidence: float
    track_id: int | None = None  # populated when using track()

    @property
    def cx(self) -> int:
        return (self.x1 + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y1 + self.y2) // 2


class YOLOModel:
    """
    Thin wrapper around ultralytics YOLO.

    Responsibilities:
    - Load / unload model weights
    - Run inference (predict) or tracking (track) on a single BGR frame
    - Return structured RawDetection list
    """

    def __init__(self) -> None:
        self._model = None
        self._model_path: str = ""
        self._weights_path: Path | None = None
        self._class_names: dict[int, str] = {}
        self._device = "cpu"
        self._use_half = False
        self._infer_lock = threading.Lock()

    # ── Public ────────────────────────────────────────────────────────────────

    def load(self, path: str | Path) -> None:
        from ultralytics import YOLO

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Weights not found: {path}")

        self._model = YOLO(str(path))
        self._model_path = path.name
        self._weights_path = path.resolve()

        self._device, self._use_half, _dev_label = _resolve_yolo_device()

        names = getattr(self._model, "names", {})
        if isinstance(names, dict):
            self._class_names = {int(i): str(n) for i, n in names.items()}
        else:
            self._class_names = {i: str(n) for i, n in enumerate(names)}

        logger.info(
            "YOLOModel: loaded %s → device=%s half=%s",
            self._model_path,
            _dev_label,
            self._use_half,
        )

    def unload(self) -> None:
        self._model = None
        self._model_path = ""
        self._weights_path = None
        self._class_names = {}

    def load_pretrained(self, name: str = "yolov8n.pt") -> None:
        """
        Load a pretrained Ultralytics model by name (auto-downloads if missing).
        Example: "yolov8n.pt"
        """
        from ultralytics import YOLO

        self._model = YOLO(str(name))
        self._model_path = str(name)
        self._weights_path = None

        self._device, self._use_half, _dev_label = _resolve_yolo_device()

        names = getattr(self._model, "names", {})
        if isinstance(names, dict):
            self._class_names = {int(i): str(n) for i, n in names.items()}
        else:
            self._class_names = {i: str(n) for i, n in enumerate(names)}

        logger.info(
            "YOLOModel: loaded pretrained %s → device=%s half=%s",
            self._model_path,
            _dev_label,
            self._use_half,
        )

    def predict(self, frame: np.ndarray, conf: float = 0.35) -> List[RawDetection]:
        """Run inference on a BGR numpy frame (detection only, no tracking)."""
        if not self.is_loaded:
            return []

        with self._infer_lock:
            results = self._model(
                frame, verbose=False, conf=conf,
                device=self._device, half=self._use_half,
                imgsz=settings.YOLO_IMGSZ,
            )[0]
            return self._parse_boxes(results)

    def track(
        self,
        frame: np.ndarray,
        conf: float = 0.35,
        tracker: str = "bytetrack.yaml",
        persist: bool = True,
    ) -> List[RawDetection]:
        """
        Run inference + built-in tracking (ByteTrack or BoT-SORT).

        Args:
            frame: BGR numpy array
            conf: confidence threshold
            tracker: "bytetrack.yaml" or "botsort.yaml"
            persist: keep track IDs across frames (must be True for continuous tracking)

        Returns:
            List of RawDetection with track_id populated
        """
        if not self.is_loaded:
            return []

        with self._infer_lock:
            results = self._model.track(
                frame,
                verbose=False,
                conf=conf,
                tracker=tracker,
                persist=persist,
                device=self._device,
                half=self._use_half,
                imgsz=settings.YOLO_IMGSZ,
            )[0]
            return self._parse_boxes(results)

    def reset_tracker(self) -> None:
        """Reset the internal tracker state (new IDs on next track() call)."""
        if self._model is not None and hasattr(self._model, "predictor"):
            predictor = self._model.predictor
            if predictor is not None and hasattr(predictor, "trackers"):
                predictor.trackers = []

    # ── Private ───────────────────────────────────────────────────────────────

    def _parse_boxes(self, results) -> List[RawDetection]:
        """Parse ultralytics Results into RawDetection list."""
        detections: List[RawDetection] = []

        for box in results.boxes:
            cls_id = int(box.cls[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])

            # track_id is available when using model.track()
            tid = None
            if box.id is not None:
                tid = int(box.id[0])

            detections.append(
                RawDetection(
                    x1=x1, y1=y1, x2=x2, y2=y2,
                    class_id=cls_id,
                    class_name=self._class_names.get(cls_id, str(cls_id)),
                    confidence=round(float(box.conf[0]), 3),
                    track_id=tid,
                )
            )

        return detections

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_path(self) -> str:
        return self._model_path

    @property
    def weights_path(self) -> Path | None:
        """
        Full path to the currently loaded weights (only for local .pt loads).
        Returns None when using load_pretrained().
        """
        return self._weights_path


# Module-level singleton
yolo_model = YOLOModel()
