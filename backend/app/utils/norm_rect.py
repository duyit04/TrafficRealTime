"""Parse normalized (0–1) crop rectangles: y1, y2, x1, x2 in frame fraction space."""

from __future__ import annotations


def parse_norm_rect(spec: str) -> tuple[float, float, float, float] | None:
    """
    Expect CSV: y1,y2,x1,x2 each in [0, 1]. Returns pixel clip bounds via to_pixel_rect.
    """
    s = (spec or "").strip()
    if not s:
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        return None
    try:
        y1, y2, x1, x2 = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError:
        return None
    for v in (y1, y2, x1, x2):
        if v < 0.0 or v > 1.0:
            return None
    if y2 <= y1 or x2 <= x1:
        return None
    return y1, y2, x1, x2


def to_pixel_rect(
    y1n: float, y2n: float, x1n: float, x2n: float, h: int, w: int
) -> tuple[int, int, int, int]:
    """Inclusive-ish OpenCV slice: y1,y2,x1,x2 as ints clipped to frame."""
    y1 = max(0, min(h - 1, int(round(y1n * h))))
    y2 = max(0, min(h, int(round(y2n * h))))
    x1 = max(0, min(w - 1, int(round(x1n * w))))
    x2 = max(0, min(w, int(round(x2n * w))))
    if y2 <= y1:
        y2 = min(h, y1 + 1)
    if x2 <= x1:
        x2 = min(w, x1 + 1)
    return y1, y2, x1, x2


def center_in_rect(cx: float, cy: float, y1: int, y2: int, x1: int, x2: int) -> bool:
    return x1 <= cx < x2 and y1 <= cy < y2
