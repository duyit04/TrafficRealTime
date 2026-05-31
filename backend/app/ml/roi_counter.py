"""
ML Layer – ROI Counter
Counts unique vehicles that enter the ROI (first time a track ID appears inside the polygon).
"""

from __future__ import annotations
from app.ml.tracker import Track


class RoiCounter:
    """
    Đếm số xe đã đi vào ROI (tính mỗi xe một lần duy nhất khi lần đầu xuất hiện trong ROI).
    """

    def __init__(self) -> None:
        self.total: int = 0
        self.by_class: dict[str, int] = {}
        self._counted_ids: set[int] = set()

    def update(self, tracks_inside_roi: list[Track]) -> None:
        for track in tracks_inside_roi:
            if track.track_id in self._counted_ids:
                continue
            self._counted_ids.add(track.track_id)
            self.total += 1
            self.by_class[track.class_name] = self.by_class.get(track.class_name, 0) + 1

    def reset(self) -> None:
        self.total = 0
        self.by_class.clear()
        self._counted_ids.clear()
