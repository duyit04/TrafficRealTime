"""
SORT — Simple Online and Realtime Tracking  (Bewley et al., 2016)
https://arxiv.org/abs/1602.00763

Motion-only tracker: Kalman filter (constant-velocity bbox) + Hungarian on IoU.
No appearance features — fast, works well when objects rarely occlude each other.

State vector  x = [cx, cy, s, r, vcx, vcy, vs]
  cx, cy  = bounding-box centre (pixels)
  s       = area  (w·h)
  r       = aspect ratio  (w/h)  — treated as constant  (vr ≈ 0)
  vcx,vcy = centre velocity  (pixels/frame)
  vs      = area velocity

Measurement   z = [cx, cy, s, r]   (4-dim)

Predict:  x̂ = F·x,      P̂ = F·P·Fᵀ + Q
Update:   K  = P̂·Hᵀ·(H·P̂·Hᵀ + R)⁻¹
          x  = x̂ + K·(z − H·x̂)
          P  = (I − K·H)·P̂

Association: Hungarian algorithm on (1 − IoU) cost matrix.
             Pairs below iou_threshold are rejected.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment
from typing import List

from app.ml.tracker import Track


# ── bbox ↔ Kalman state helpers ───────────────────────────────────────────────

def _bbox_to_z(b) -> np.ndarray:
    """[x1,y1,x2,y2] → column vector [cx, cy, s, r]"""
    w = float(b[2] - b[0])
    h = max(float(b[3] - b[1]), 1.0)
    return np.array([[b[0] + w / 2], [b[1] + h / 2], [w * h], [w / h]], dtype=float)


def _z_to_bbox(x) -> np.ndarray:
    """Kalman state [cx, cy, s, r, ...] → [x1, y1, x2, y2]"""
    # Use ravel() so scalar extraction works with both (7,) and (7,1) shapes (NumPy 2.x)
    xf = np.ravel(x)
    s, r = float(xf[2]), float(xf[3])
    if s <= 0 or r <= 0:
        return np.zeros(4)
    w = np.sqrt(max(s * r, 0.0))
    h = s / w if w > 0 else 1.0
    cx, cy = float(xf[0]), float(xf[1])
    return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])


def _iou_batch(a: list, b: list) -> np.ndarray:
    """Vectorised IoU matrix, shape (len(a), len(b))."""
    if not a or not b:
        return np.zeros((len(a), len(b)))
    aa = np.array(a, dtype=float)
    bb = np.array(b, dtype=float)
    xi1 = np.maximum(aa[:, None, 0], bb[None, :, 0])
    yi1 = np.maximum(aa[:, None, 1], bb[None, :, 1])
    xi2 = np.minimum(aa[:, None, 2], bb[None, :, 2])
    yi2 = np.minimum(aa[:, None, 3], bb[None, :, 3])
    inter = np.maximum(0.0, xi2 - xi1) * np.maximum(0.0, yi2 - yi1)
    area_a = (aa[:, 2] - aa[:, 0]) * (aa[:, 3] - aa[:, 1])
    area_b = (bb[:, 2] - bb[:, 0]) * (bb[:, 3] - bb[:, 1])
    union = area_a[:, None] + area_b[None, :] - inter
    return np.where(union > 0, inter / union, 0.0)


# ── Single-object Kalman box ──────────────────────────────────────────────────

class _KalmanBox:
    """Constant-velocity Kalman filter for one bounding box."""

    # Shared read-only matrices (built once at class load)
    _F = np.eye(7, dtype=float)
    _F[0, 4] = _F[1, 5] = _F[2, 6] = 1.0   # position += velocity * dt (dt=1 frame)
    _H = np.eye(4, 7, dtype=float)            # observe [cx, cy, s, r]
    _Q = np.diag([1.0, 1.0, 1.0, 1.0, 0.01, 0.01, 1e-4]).astype(float)  # process noise
    _R = np.diag([1.0, 1.0, 10.0, 10.0]).astype(float)                   # measurement noise

    _counter: int = 0   # global monotonic ID (never reset — avoids multi-camera collisions)

    def __init__(self, bbox, class_name: str = "") -> None:
        self.x = np.zeros((7, 1), dtype=float)
        self.x[:4] = _bbox_to_z(bbox)
        # High initial uncertainty on velocity
        self.P = np.diag([10., 10., 10., 10., 1e4, 1e4, 1e4]).astype(float)
        self.time_since_update = 0
        self.hits = 0
        self.hit_streak = 0
        self.id = _KalmanBox._counter
        _KalmanBox._counter += 1
        self.class_name = class_name

    def predict(self) -> np.ndarray:
        if float(self.x.flat[2]) + float(self.x.flat[6]) <= 0:
            self.x.flat[6] = 0.0
        self.x = self._F @ self.x
        self.P = self._F @ self.P @ self._F.T + self._Q
        if self.time_since_update > 0:
            self.hit_streak = 0
        self.time_since_update += 1
        return _z_to_bbox(self.x)

    def update(self, bbox, class_name: str = "") -> None:
        z = _bbox_to_z(bbox)
        S = self._H @ self.P @ self._H.T + self._R
        K = self.P @ self._H.T @ np.linalg.inv(S)
        self.x += K @ (z - self._H @ self.x)
        self.P = (np.eye(7) - K @ self._H) @ self.P
        self.time_since_update = 0
        self.hits += 1
        self.hit_streak += 1
        if class_name:
            self.class_name = class_name

    def get_state(self) -> np.ndarray:
        return _z_to_bbox(self.x)


# ── SORT multi-object tracker ─────────────────────────────────────────────────

class SortTracker:
    """
    SORT multi-object tracker adapter.

    Implements the same interface as BuiltinTracker.
    When uses_builtin is False, stream_service calls yolo_model.predict()
    (detection only) and passes the raw detections here for ID assignment.
    """
    uses_builtin: bool = False
    tracker_yaml: None = None

    def __init__(
        self,
        max_age: int = 30,
        min_hits: int = 3,
        iou_threshold: float = 0.3,
    ) -> None:
        """
        max_age       : frames a track survives without matching a detection
        min_hits      : consecutive hits required before a track is output
        iou_threshold : min IoU to accept a detection–track assignment
        """
        self.max_age = max_age
        self.min_hits = min_hits
        self.iou_threshold = iou_threshold
        self._trackers: List[_KalmanBox] = []
        self._frame_count: int = 0
        self._prev_cy: dict[int, float] = {}
        self._prev_cx: dict[int, float] = {}

    # ── Public ──────────────────────────────────────────────────────────────

    def update(self, detections: list, frame=None) -> List[Track]:
        """
        detections : List[RawDetection] from yolo_model.predict() — track_id is None
        frame      : not used by SORT (no appearance features)
        returns    : confirmed Track list with stable IDs
        """
        self._frame_count += 1

        # Predict — advance each Kalman filter one step
        predicted: List[np.ndarray] = []
        dead: List[_KalmanBox] = []
        for t in self._trackers:
            p = t.predict()
            if np.any(np.isnan(p)):
                dead.append(t)
            else:
                predicted.append(p)
        for t in dead:
            self._trackers.remove(t)

        det_boxes = [[d.x1, d.y1, d.x2, d.y2] for d in detections]

        # Associate detections ↔ predictions via Hungarian on IoU
        matched, unmatched_dets, _ = self._associate(predicted, det_boxes)

        for di, ti in matched:
            self._trackers[ti].update(det_boxes[di], detections[di].class_name)

        for di in unmatched_dets:
            self._trackers.append(_KalmanBox(det_boxes[di], detections[di].class_name))

        # Build output — emit only confirmed tracks; remove dead ones
        result: List[Track] = []
        active_ids: set[int] = set()
        i = len(self._trackers)
        for trk in reversed(self._trackers):
            i -= 1
            confirmed = trk.time_since_update < 1 and (
                trk.hit_streak >= self.min_hits or self._frame_count <= self.min_hits
            )
            if confirmed:
                b = trk.get_state()
                x1, y1 = max(0.0, b[0]), max(0.0, b[1])
                x2, y2 = b[2], b[3]
                cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                tid = trk.id
                active_ids.add(tid)
                result.append(Track(
                    track_id=tid,
                    cx=cx, cy=cy,
                    class_name=trk.class_name,
                    prev_cy=self._prev_cy.get(tid, -1.0),
                    prev_cx=self._prev_cx.get(tid, -1.0),
                    x1=x1, y1=y1, x2=x2, y2=y2,
                ))
                self._prev_cy[tid] = cy
                self._prev_cx[tid] = cx
            if trk.time_since_update > self.max_age:
                self._trackers.pop(i)

        for k in list(self._prev_cy):
            if k not in active_ids:
                self._prev_cy.pop(k, None)
                self._prev_cx.pop(k, None)

        return result

    def reset(self) -> None:
        self._trackers.clear()
        self._frame_count = 0
        self._prev_cy.clear()
        self._prev_cx.clear()

    # ── Private ──────────────────────────────────────────────────────────────

    def _associate(
        self, predicted: list, det_boxes: list
    ) -> tuple[list, list, list]:
        if not predicted or not det_boxes:
            return [], list(range(len(det_boxes))), list(range(len(predicted)))

        iou = _iou_batch(det_boxes, predicted)
        row_ind, col_ind = linear_sum_assignment(1.0 - iou)

        matched, matched_dets, matched_trks = [], set(), set()
        for r, c in zip(row_ind, col_ind):
            if iou[r, c] >= self.iou_threshold:
                matched.append((r, c))
                matched_dets.add(r)
                matched_trks.add(c)

        return (
            matched,
            [i for i in range(len(det_boxes)) if i not in matched_dets],
            [i for i in range(len(predicted)) if i not in matched_trks],
        )
