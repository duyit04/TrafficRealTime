"""
Detection Service – business logic for running detection on a single frame.
Calls ML layer (YOLOModel + RoiFilter), does NOT touch ultralytics directly.
"""

from __future__ import annotations
import numpy as np

from app.core.logger import logger
from app.ml.yolo_model import yolo_model, RawDetection
from app.models.detection_model import BoundingBox, Detection


class DetectionService:
    """
    Orchestrates inference for one frame:
      1. YOLOModel.predict()   ← ml layer
      2. filter_by_roi()       ← ml layer
      3. Map to API models     ← models layer

    Service layer knows WHAT to do; ml layer knows HOW.
    """

    def detect(self, frame: np.ndarray, conf: float) -> list[Detection]:
        """Run YOLO on one BGR frame; return Pydantic Detection list for API responses."""
        raw: list[RawDetection] = yolo_model.predict(frame, conf)
        return [
            Detection(
                bbox=BoundingBox(x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2),
                class_name=d.class_name,
                confidence=d.confidence,
            )
            for d in raw
        ]

    def detect_raw(self, frame: np.ndarray, conf: float) -> list[RawDetection]:
        """Run YOLO on one BGR frame; return RawDetection list (has .cx/.cy for tracking)."""
        return yolo_model.predict(frame, conf)


# Singleton
detection_service = DetectionService()
