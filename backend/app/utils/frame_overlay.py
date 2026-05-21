"""Draw detection UI on BGR frames (server-side burn-in for H264)."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

# BGR colors — mirror CounterPanel.tsx CLASS_COLORS (hex → BGR)
_CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "car":              (235,  99,  37),  # #2563eb → B235 G99  R37
    "truck":            (105,  85,  71),  # #475569 → B105 G85  R71
    "bus":              (233, 165,  14),  # #0ea5e9 → B233 G165 R14
    "motorcycle":       (139, 116, 100),  # #64748b → B139 G116 R100
    "motorbike":        (139, 116, 100),  # #64748b
    "bicycle":          (178, 145,   8),  # #0891b2 → B178 G145 R8
    "person":           (241, 102,  99),  # #6366f1 → B241 G102 R99
    "pedestrian":       (241, 102,  99),  # #6366f1
    "container truck":  ( 85,  65,  51),  # #334155 → B85  G65  R51
}
_DEFAULT_COLOR: tuple[int, int, int] = (180, 180, 180)  # light-gray


def _color_for(class_name: str) -> tuple[int, int, int]:
    key = class_name.lower()
    return _CLASS_COLORS.get(key, _DEFAULT_COLOR)


def overlay_traffic_ui(
    frame_bgr: np.ndarray,
    detections: list[dict[str, Any]],
    *,
    line_y_px: int,
    show_line: bool,
    in_place: bool = False,
) -> np.ndarray:
    """Draw boxes + optional counting line on BGR frame (copy unless in_place)."""
    out = frame_bgr if in_place else frame_bgr.copy()
    h, w = out.shape[:2]
    for d in detections or []:
        bb = d.get("bbox") if isinstance(d, dict) else None
        if not isinstance(bb, dict) and isinstance(d, dict):
            if all(k in d for k in ("x1", "y1", "x2", "y2")):
                bb = {"x1": d["x1"], "y1": d["y1"], "x2": d["x2"], "y2": d["y2"]}
        if not isinstance(bb, dict):
            continue
        try:
            x1 = int(bb["x1"])
            y1 = int(bb["y1"])
            x2 = int(bb["x2"])
            y2 = int(bb["y2"])
        except (KeyError, TypeError, ValueError):
            continue
        x1 = max(0, min(x1, w - 1))
        x2 = max(0, min(x2, w))
        y1 = max(0, min(y1, h - 1))
        y2 = max(0, min(y2, h))
        if x2 <= x1 or y2 <= y1:
            continue
        label = str(d.get("class_name") or "?")
        color = _color_for(label)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        tid = d.get("track_id")
        conf = float(d.get("confidence") or 0.0)
        text = label + (f" ID{tid}" if tid is not None else "") + f" {conf * 100:.0f}%"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        ty = max(th + 6, y1)
        cv2.rectangle(out, (x1, ty - th - 6), (x1 + tw + 6, ty), color, -1)
        cv2.putText(out, text, (x1 + 3, ty - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
    if show_line and 0 <= line_y_px < h:
        cv2.line(out, (0, line_y_px), (w - 1, line_y_px), (0, 255, 255), 2)
    return out
