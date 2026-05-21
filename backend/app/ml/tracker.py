"""
ML Layer – Tracking adapters.

Two modes:
  1. Built-in (default): Uses ultralytics model.track() with ByteTrack/BoT-SORT.
     No external tracker needed — track IDs come directly from YOLO.
  2. Legacy: SORT / DeepSORT via external packages (kept for compatibility).

The stream_service uses BuiltinTracker by default.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Any

import numpy as np


@dataclass
class Track:
    """A tracked object with stable ID."""
    track_id: int
    cx: float
    cy: float
    class_name: str
    prev_cx: float = -1.0
    prev_cy: float = 0.0
    age: int = 0
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    det_index: int | None = None


class BuiltinTracker:
    """
    Uses ultralytics built-in tracking (ByteTrack / BoT-SORT).
    No external dependencies needed — tracking happens inside model.track().
    This adapter just converts RawDetection (with track_id) → Track objects.
    """
    uses_builtin: bool = True

    def __init__(self, tracker_yaml: str = "bytetrack.yaml", **kwargs: Any) -> None:
        self.tracker_yaml = tracker_yaml
        self._id_to_prev_cy: dict[int, float] = {}
        self._id_to_prev_cx: dict[int, float] = {}

    def update(self, detections: list, frame: np.ndarray | None = None) -> List[Track]:
        """
        Convert RawDetection list (already tracked by ultralytics) → Track list.
        detections should come from yolo_model.track(), which populates track_id.
        """
        tracks = []
        active_ids = set()

        for i, d in enumerate(detections):
            tid = d.track_id
            if tid is None:
                continue

            active_ids.add(tid)
            cx, cy = d.cx, d.cy
            # Use sentinel -1.0 for new tracks so prev_cy != cy
            # This avoids the lo==hi skip in VehicleCounter on first frame.
            # A new track that appears above the line will have prev_cy=-1 (top),
            # so crossing is detected correctly on the very next frame.
            prev_cy = self._id_to_prev_cy.get(tid, -1.0)
            self._id_to_prev_cy[tid] = cy
            prev_cx = self._id_to_prev_cx.get(tid, -1.0)
            self._id_to_prev_cx[tid] = cx

            tracks.append(
                Track(
                    track_id=tid,
                    cx=cx, cy=cy,
                    class_name=d.class_name,
                    prev_cx=prev_cx,
                    prev_cy=prev_cy,
                    x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2,
                    det_index=i,
                )
            )

        # Prune old IDs
        for key in list(self._id_to_prev_cy.keys()):
            if key not in active_ids:
                self._id_to_prev_cy.pop(key, None)
        for key in list(self._id_to_prev_cx.keys()):
            if key not in active_ids:
                self._id_to_prev_cx.pop(key, None)

        return tracks

    def reset(self) -> None:
        self._id_to_prev_cy.clear()
        self._id_to_prev_cx.clear()


def get_tracker(tracker_type: str, **kwargs: Any):
    """
    Factory: returns a tracker adapter.

    tracker_type:
      - "bytetrack" → ultralytics built-in ByteTrack (default)
      - "botsort"   → ultralytics built-in BoT-SORT
      - "sort"      → pure Python SORT (Kalman + Hungarian IoU)
      - "deepsort"  → DeepSORT (SORT + HSV appearance matching)
    """
    t = (tracker_type or "bytetrack").strip().lower()
    if t == "bytetrack":
        return BuiltinTracker(tracker_yaml="bytetrack.yaml", **kwargs)
    if t == "botsort":
        return BuiltinTracker(tracker_yaml="botsort.yaml", **kwargs)
    if t == "sort":
        from app.ml.sort_tracker import SortTracker
        return SortTracker(**kwargs)
    if t == "deepsort":
        from app.ml.deep_sort_tracker import DeepSortTracker
        return DeepSortTracker(**kwargs)
    # Fallback to bytetrack
    return BuiltinTracker(tracker_yaml="bytetrack.yaml", **kwargs)
