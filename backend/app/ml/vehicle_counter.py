"""
ML Layer – Vehicle Counter
Counts vehicles that cross a horizontal counting line.

Two modes:
  - "all":       count every crossing regardless of direction (default)
  - "direction":  separate IN (top→bottom) vs OUT (bottom→top)
"""

from __future__ import annotations
from app.ml.tracker import Track


class VehicleCounter:
    """
    Stateful counter: tracks which track IDs have already been counted
    to avoid double-counting on subsequent frames.

    Modes:
      - "all":       mọi xe qua line đều +1 total (không phân biệt chiều)
      - "direction":  tách riêng IN / OUT

    Directions (camera nhìn từ trên xuống):
      - IN  = xe di chuyển từ trên xuống dưới (prev_cy < cy, qua line)
      - OUT = xe di chuyển từ dưới lên trên   (prev_cy > cy, qua line)
    """

    def __init__(self, mode: str = "all") -> None:
        self.mode: str = mode          # "all" | "direction"
        self.total: int = 0
        self.count_in: int = 0
        self.count_out: int = 0
        self.by_class: dict[str, int] = {}
        self.by_class_in: dict[str, int] = {}
        self.by_class_out: dict[str, int] = {}
        self._counted_ids: set[int] = set()

    def update(self, tracks: list[Track], line_y: float) -> None:
        for track in tracks:
            if track.track_id in self._counted_ids:
                continue

            # Kiểm tra xe có vượt qua line giữa prev_cy và cy không
            # Dùng min/max thay vì strict < để không bỏ sót trường hợp biên
            lo = min(track.prev_cy, track.cy)
            hi = max(track.prev_cy, track.cy)

            # prev_cy == cy → xe chưa di chuyển (frame đầu tiên) → skip
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

            if self.mode == "direction":
                # Phân biệt chiều: IN = xuống, OUT = lên
                if track.cy > track.prev_cy:
                    self.count_in += 1
                    self.by_class_in[track.class_name] = (
                        self.by_class_in.get(track.class_name, 0) + 1
                    )
                else:
                    self.count_out += 1
                    self.by_class_out[track.class_name] = (
                        self.by_class_out.get(track.class_name, 0) + 1
                    )

    def set_mode(self, mode: str) -> None:
        if mode in ("all", "direction"):
            self.mode = mode

    def reset(self) -> None:
        self.total = 0
        self.count_in = 0
        self.count_out = 0
        self.by_class.clear()
        self.by_class_in.clear()
        self.by_class_out.clear()
        self._counted_ids.clear()
