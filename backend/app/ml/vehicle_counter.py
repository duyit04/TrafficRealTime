"""
ML Layer – Vehicle Counter
Counts vehicles that cross a horizontal counting line.
"""

from __future__ import annotations
from app.ml.tracker import Track


class VehicleCounter:
    """
    Stateful counter: tracks which track IDs have already been counted
    to avoid double-counting on subsequent frames.
    """

    def __init__(self) -> None:
        self.total: int = 0
        self.by_class: dict[str, int] = {}
        self._counted_ids: set[int] = set()

    def update(self, tracks: list[Track], line_y: float) -> None:
        for track in tracks:
            if track.track_id in self._counted_ids:
                continue

            lo = min(track.prev_cy, track.cy)
            hi = max(track.prev_cy, track.cy)

            if lo == hi:
                continue

            crossed = lo < line_y <= hi or lo <= line_y < hi

            if not crossed:
                continue

            self._counted_ids.add(track.track_id)
            self.total += 1
            self.by_class[track.class_name] = (
                self.by_class.get(track.class_name, 0) + 1
            )

    def reset(self) -> None:
        self.total = 0
        self.by_class.clear()
        self._counted_ids.clear()
