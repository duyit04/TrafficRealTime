"""
ROI Service – manages ROI state (polygon + active flag).
Delegates geometric logic to app.ml.roi_filter.
"""

from __future__ import annotations
from app.core.logger import logger
from app.ml.roi_filter import point_in_polygon


def _normalize_slot(slot: str | int | None) -> str:
    """
    Normalize ROI slot keys.

    Supported:
      - None / "primary" / "0" / 0 → "primary"
      - "companion" / "1"         → "companion"
      - 2 / "2"                   → "2"
      - 3 / "3"                   → "3"
    """
    if slot is None:
        return "primary"
    if isinstance(slot, int):
        if slot == 0:
            return "primary"
        if slot == 1:
            return "companion"
        return str(slot)
    s = str(slot).strip().lower()
    if s in ("primary", "0", "main", "cam1", "camera1"):
        return "primary"
    if s in ("companion", "1", "cam2", "camera2"):
        return "companion"
    if s in ("2", "3"):
        return s
    # Allow future extension (e.g. "slot2") but keep it deterministic.
    if s.startswith("slot"):
        tail = s[4:].strip()
        if tail in ("2", "3"):
            return tail
    raise ValueError("Invalid ROI slot. Use primary|companion|2|3.")


class RoiService:
    """
    Stateful service: stores ROI polygon + active flag PER slot.
    Geometric computation is in ml.roi_filter – no math here.
    """

    def __init__(self) -> None:
        self._points_by_slot: dict[str, list[tuple[int, int]]] = {}
        self._active_by_slot: dict[str, bool] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def set_roi(self, points: list[list[float]], active: bool = True, slot: str | int | None = None) -> None:
        key = _normalize_slot(slot)
        pts = [(int(p[0]), int(p[1])) for p in points]
        act = bool(active) and len(pts) >= 3
        self._points_by_slot[key] = pts
        self._active_by_slot[key] = act
        logger.info("ROI[%s] updated: %d pts, active=%s", key, len(pts), act)

    def clear(self, slot: str | int | None = None) -> None:
        key = _normalize_slot(slot)
        self._points_by_slot.pop(key, None)
        self._active_by_slot[key] = False
        logger.info("ROI[%s] cleared", key)

    def is_inside(self, cx: float, cy: float, slot: str | int | None = None) -> bool:
        """Quick check used by legacy callers. Delegates to ml layer."""
        key = _normalize_slot(slot)
        pts = self._points_by_slot.get(key, [])
        act = self._active_by_slot.get(key, False)
        if not act or len(pts) < 3:
            return True
        return point_in_polygon(cx, cy, pts)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def points(self) -> list[tuple[int, int]]:
        """Back-compat: primary slot points."""
        return self._points_by_slot.get("primary", [])

    def points_for(self, slot: str | int | None = None) -> list[tuple[int, int]]:
        key = _normalize_slot(slot)
        return self._points_by_slot.get(key, [])

    @property
    def active(self) -> bool:
        """Back-compat: primary slot active flag."""
        return bool(self._active_by_slot.get("primary", False))

    def active_for(self, slot: str | int | None = None) -> bool:
        key = _normalize_slot(slot)
        return bool(self._active_by_slot.get(key, False))

    @property
    def mid_y(self) -> float | None:
        """Back-compat: primary slot mid_y."""
        return self.mid_y_for("primary")

    def mid_y_for(self, slot: str | int | None = None) -> float | None:
        """Tọa độ Y giữa vùng ROI — dùng làm counting line khi ROI active."""
        key = _normalize_slot(slot)
        pts = self._points_by_slot.get(key, [])
        act = self._active_by_slot.get(key, False)
        if not act or len(pts) < 3:
            return None
        ys = [p[1] for p in pts]
        return (min(ys) + max(ys)) / 2.0

    @property
    def line_position_ratio(self) -> float | None:
        """Tỷ lệ Y giữa ROI / frame height. None nếu không active."""
        return None  # cần frame height, tính ở stream_service


# Singleton
roi_service = RoiService()
