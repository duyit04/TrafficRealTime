"""
DeepSORT — Deep SORT with Appearance Matching  (Wojke et al., 2017)
https://arxiv.org/abs/1703.07402

Extends SORT with an appearance descriptor to survive occlusions and reduce
ID switches when two objects cross or briefly overlap.

Key additions over SORT
───────────────────────
1. Appearance feature — HSV colour histogram from each detection crop.
   Original paper uses a 128-d CNN Re-ID embedding trained on pedestrian data.
   For traffic cameras, HSV histograms are lightweight and well-suited: vehicles
   viewed from a fixed overhead/oblique angle have distinctive colour distributions.

2. Per-track feature gallery — rolling buffer of the last gallery_size features.
   Matching cost = min cosine distance from detection feature to gallery.

3. Cascade matching (three passes):
     Pass 1 — confirmed tracks vs all detections.
              Combined cost: α·appearance + (1−α)·IoU_cost.
              Gate: pairs where appearance > max_cosine_dist AND IoU < iou_threshold
              are forbidden (cost set to ∞).
     Pass 2 — unmatched confirmed tracks vs remaining detections, IoU only.
     Pass 3 — tentative tracks vs still-unmatched detections, IoU only.

4. Track state machine:
     tentative → confirmed  after n_init consecutive hits
     confirmed → deleted    after max_age consecutive misses
     tentative → deleted    after 1 miss (stricter — avoids false IDs)

Appearance threshold: even if IoU is high, a pair exceeding max_cosine_dist is
rejected unless the track has no gallery yet — prevents Re-ID confusion between
two nearby vehicles of different colours.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment
from typing import List, Optional

import cv2

from app.ml.tracker import Track
from app.ml.sort_tracker import _bbox_to_z, _z_to_bbox, _iou_batch


# ── Appearance feature ────────────────────────────────────────────────────────

_FEAT_DIM = 96   # H:36 + S:32 + V:28

def _extract_hsv_hist(frame: np.ndarray, bbox) -> np.ndarray:
    """
    96-dim normalised HSV histogram from a detection crop.
    Returns zero vector when frame is None or crop is degenerate.
    """
    if frame is None:
        return np.zeros(_FEAT_DIM, dtype=float)
    x1, y1 = int(max(0, bbox[0])), int(max(0, bbox[1]))
    x2, y2 = int(bbox[2]), int(bbox[3])
    if x2 <= x1 or y2 <= y1:
        return np.zeros(_FEAT_DIM, dtype=float)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return np.zeros(_FEAT_DIM, dtype=float)
    crop = cv2.resize(crop, (64, 128))
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0], None, [36], [0, 180]).flatten()
    s = cv2.calcHist([hsv], [1], None, [32], [0, 256]).flatten()
    v = cv2.calcHist([hsv], [2], None, [28], [0, 256]).flatten()
    feat = np.concatenate([h, s, v])
    n = np.linalg.norm(feat)
    return (feat / n) if n > 1e-6 else feat


def _cosine_dist(q: np.ndarray, g: np.ndarray) -> np.ndarray:
    """
    Cosine distance matrix (N_query × N_gallery) ∈ [0, 2].
    Lower = more similar.
    """
    if q.shape[0] == 0 or g.shape[0] == 0:
        return np.zeros((len(q), len(g)))
    qn = q / (np.linalg.norm(q, axis=1, keepdims=True) + 1e-6)
    gn = g / (np.linalg.norm(g, axis=1, keepdims=True) + 1e-6)
    return 1.0 - qn @ gn.T


# ── Track states ──────────────────────────────────────────────────────────────

_TENTATIVE = 0
_CONFIRMED = 1
_DELETED   = 2


# ── Single-object Kalman filter (same motion model as SORT, no ID counter) ───

class _DSKalmanBox:
    """Constant-velocity Kalman filter for DeepSORT (no global ID counter)."""

    _F = np.eye(7, dtype=float)
    _F[0, 4] = _F[1, 5] = _F[2, 6] = 1.0
    _H = np.eye(4, 7, dtype=float)
    _Q = np.diag([1.0, 1.0, 1.0, 1.0, 0.01, 0.01, 1e-4]).astype(float)
    _R = np.diag([1.0, 1.0, 10.0, 10.0]).astype(float)

    def __init__(self, bbox, class_name: str = "") -> None:
        self.x = np.zeros((7, 1), dtype=float)
        self.x[:4] = _bbox_to_z(bbox)
        self.P = np.diag([10., 10., 10., 10., 1e4, 1e4, 1e4]).astype(float)
        self.time_since_update = 0
        self.hits = 0
        self.hit_streak = 0
        self.class_name = class_name

    def predict(self) -> None:
        if float(self.x.flat[2]) + float(self.x.flat[6]) <= 0:
            self.x.flat[6] = 0.0
        self.x = self._F @ self.x
        self.P = self._F @ self.P @ self._F.T + self._Q
        if self.time_since_update > 0:
            self.hit_streak = 0
        self.time_since_update += 1

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


# ── Single-object DeepSORT track ──────────────────────────────────────────────

class _DeepTrack:
    """Single-object track: Kalman motion model + appearance feature gallery."""

    _counter: int = 0   # global monotonic ID

    def __init__(
        self,
        bbox,
        class_name: str = "",
        feature: Optional[np.ndarray] = None,
        n_init: int = 3,
        max_age: int = 30,
        gallery_size: int = 50,
    ) -> None:
        self._kf = _DSKalmanBox(bbox, class_name)
        self.id = _DeepTrack._counter
        _DeepTrack._counter += 1
        self.n_init = n_init
        self.max_age = max_age
        self.gallery_size = gallery_size
        self.gallery: List[np.ndarray] = []
        self.state = _TENTATIVE
        self.class_name = class_name
        if feature is not None:
            self.gallery.append(feature)

    def predict(self) -> None:
        self._kf.predict()

    def update(self, bbox, class_name: str = "", feature: Optional[np.ndarray] = None) -> None:
        self._kf.update(bbox, class_name)
        if class_name:
            self.class_name = class_name
        if feature is not None:
            self.gallery.append(feature)
            if len(self.gallery) > self.gallery_size:
                self.gallery.pop(0)
        if self._kf.hit_streak >= self.n_init:
            self.state = _CONFIRMED

    def mark_missed(self) -> None:
        """Called when no detection was matched this frame."""
        if self.state == _TENTATIVE:
            self.state = _DELETED   # tentative tracks die immediately on first miss
        elif self._kf.time_since_update > self.max_age:
            self.state = _DELETED

    def get_state(self) -> np.ndarray:
        return _z_to_bbox(self._kf.x)

    @property
    def time_since_update(self) -> int:
        return self._kf.time_since_update

    @classmethod
    def reset_count(cls) -> None:
        cls._counter = 0


# ── DeepSORT multi-object tracker ─────────────────────────────────────────────

class DeepSortTracker:
    """
    DeepSORT multi-object tracker adapter.

    Implements the same interface as BuiltinTracker.
    When uses_builtin is False, stream_service calls yolo_model.predict()
    and passes (detections, frame) here for ID assignment with appearance matching.
    """
    uses_builtin: bool = False
    tracker_yaml: None = None

    def __init__(
        self,
        max_age: int = 30,
        n_init: int = 3,
        max_cosine_dist: float = 0.4,
        iou_threshold: float = 0.3,
        appearance_weight: float = 0.5,
        gallery_size: int = 50,
    ) -> None:
        """
        max_age          : max frames without match before a confirmed track is deleted
        n_init           : consecutive hits to confirm a new track
        max_cosine_dist  : appearance gate — pairs above this are forbidden (unless no gallery)
        iou_threshold    : min IoU to accept a detection–track pair
        appearance_weight: blend factor α  (0 = IoU-only ≡ SORT,  1 = appearance-only)
        gallery_size     : max stored features per track
        """
        self.max_age = max_age
        self.n_init = n_init
        self.max_cosine_dist = max_cosine_dist
        self.iou_threshold = iou_threshold
        self.appearance_weight = appearance_weight
        self.gallery_size = gallery_size
        self._tracks: List[_DeepTrack] = []
        self._prev_cy: dict[int, float] = {}
        self._prev_cx: dict[int, float] = {}

    # ── Public ───────────────────────────────────────────────────────────────

    def update(self, detections: list, frame: Optional[np.ndarray] = None) -> List[Track]:
        """
        detections : List[RawDetection] from yolo_model.predict() — track_id is None
        frame      : BGR numpy frame for appearance feature extraction
        returns    : confirmed Track list with stable IDs
        """
        # 1. Predict all tracks forward one step
        for trk in self._tracks:
            trk.predict()

        # 2. Extract HSV features and bbox list for all detections
        det_boxes: List[list] = []
        det_feats: List[np.ndarray] = []
        for d in detections:
            b = [d.x1, d.y1, d.x2, d.y2]
            det_boxes.append(b)
            det_feats.append(_extract_hsv_hist(frame, b))
        N = len(detections)

        # 3. Partition tracks by state
        conf_idxs = [i for i, t in enumerate(self._tracks) if t.state == _CONFIRMED]
        tent_idxs = [i for i, t in enumerate(self._tracks) if t.state == _TENTATIVE]

        matched_det: set[int] = set()
        matched_trk: set[int] = set()

        def _apply_matches(matches, trk_global_idxs):
            for di, tl in matches:
                ti = trk_global_idxs[tl]
                self._tracks[ti].update(
                    det_boxes[di], detections[di].class_name, det_feats[di]
                )
                matched_det.add(di)
                matched_trk.add(ti)

        # Pass 1 — confirmed tracks, appearance + IoU blended cost
        avail = list(range(N))
        if conf_idxs and avail:
            m1, _, _ = self._match_appearance(
                [self._tracks[i] for i in conf_idxs],
                det_boxes, det_feats, avail
            )
            _apply_matches(m1, conf_idxs)
            avail = [i for i in range(N) if i not in matched_det]

        # Pass 2 — unmatched confirmed tracks, IoU only
        unmatched_conf = [i for i in conf_idxs if i not in matched_trk]
        if unmatched_conf and avail:
            m2, _, _ = self._match_iou(
                [self._tracks[i] for i in unmatched_conf], det_boxes, avail
            )
            _apply_matches(m2, unmatched_conf)
            avail = [i for i in range(N) if i not in matched_det]

        # Pass 3 — tentative tracks, IoU only
        if tent_idxs and avail:
            m3, _, _ = self._match_iou(
                [self._tracks[i] for i in tent_idxs], det_boxes, avail
            )
            _apply_matches(m3, tent_idxs)

        # 4. Mark unmatched tracks as missed
        for i, trk in enumerate(self._tracks):
            if i not in matched_trk:
                trk.mark_missed()

        # 5. Spawn new tentative tracks for unmatched detections
        for di in range(N):
            if di not in matched_det:
                self._tracks.append(_DeepTrack(
                    det_boxes[di],
                    detections[di].class_name,
                    det_feats[di],
                    n_init=self.n_init,
                    max_age=self.max_age,
                    gallery_size=self.gallery_size,
                ))

        # 6. Prune deleted tracks
        self._tracks = [t for t in self._tracks if t.state != _DELETED]

        # 7. Build output — only confirmed tracks updated this frame
        result: List[Track] = []
        active_ids: set[int] = set()
        for trk in self._tracks:
            if trk.state == _CONFIRMED and trk.time_since_update == 0:
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

        for k in list(self._prev_cy):
            if k not in active_ids:
                self._prev_cy.pop(k, None)
                self._prev_cx.pop(k, None)

        return result

    def reset(self) -> None:
        self._tracks.clear()
        self._prev_cy.clear()
        self._prev_cx.clear()

    # ── Private ───────────────────────────────────────────────────────────────

    def _match_appearance(
        self,
        tracks: List[_DeepTrack],
        det_boxes: list,
        det_feats: list,
        det_indices: list,
    ) -> tuple:
        """
        Combined appearance + IoU cost matching.
        Returns (matched, unmatched_det_indices, unmatched_trk_local_indices).
        """
        if not tracks or not det_indices:
            return [], det_indices, list(range(len(tracks)))

        # Appearance cost: min cosine dist from detection to track gallery
        sub_feats = np.array([det_feats[i] for i in det_indices])  # (M, D)
        app_cost = np.ones((len(det_indices), len(tracks)), dtype=float)
        for j, trk in enumerate(tracks):
            if trk.gallery:
                gallery_arr = np.array(trk.gallery)            # (G, D)
                d = _cosine_dist(sub_feats, gallery_arr)       # (M, G)
                app_cost[:, j] = np.min(d, axis=1)

        # IoU cost
        sub_boxes = [det_boxes[i] for i in det_indices]
        iou = _iou_batch(sub_boxes, [t.get_state() for t in tracks])
        iou_cost = 1.0 - iou

        # Blended cost
        w = self.appearance_weight
        cost = w * app_cost + (1.0 - w) * iou_cost

        # Hard gate: reject pairs where both appearance AND IoU are poor
        gate = (app_cost > self.max_cosine_dist) & (iou < self.iou_threshold)
        # Tracks with empty gallery skip the appearance gate (no prior info)
        for j, trk in enumerate(tracks):
            if not trk.gallery:
                gate[:, j] = False
        cost[gate] = 1e5

        row_ind, col_ind = linear_sum_assignment(cost)
        matched, m_rows, m_cols = [], set(), set()
        for r, c in zip(row_ind, col_ind):
            if cost[r, c] < 1e4:
                matched.append((det_indices[r], c))
                m_rows.add(r)
                m_cols.add(c)

        return (
            matched,
            [det_indices[i] for i in range(len(det_indices)) if i not in m_rows],
            [i for i in range(len(tracks)) if i not in m_cols],
        )

    def _match_iou(
        self,
        tracks: List[_DeepTrack],
        det_boxes: list,
        det_indices: list,
    ) -> tuple:
        """IoU-only matching for a subset of tracks and detections."""
        if not tracks or not det_indices:
            return [], det_indices, list(range(len(tracks)))

        sub_boxes = [det_boxes[i] for i in det_indices]
        iou = _iou_batch(sub_boxes, [t.get_state() for t in tracks])
        row_ind, col_ind = linear_sum_assignment(1.0 - iou)
        matched, m_rows, m_cols = [], set(), set()
        for r, c in zip(row_ind, col_ind):
            if iou[r, c] >= self.iou_threshold:
                matched.append((det_indices[r], c))
                m_rows.add(r)
                m_cols.add(c)

        return (
            matched,
            [det_indices[i] for i in range(len(det_indices)) if i not in m_rows],
            [i for i in range(len(tracks)) if i not in m_cols],
        )
