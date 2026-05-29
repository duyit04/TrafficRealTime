"""
Detection routes – ROI, stats, settings, timeline, congestion.
"""

from __future__ import annotations
from fastapi import APIRouter

from app.models.detection_model import (
    RoiRequest, SettingsUpdate, SuccessResponse, VehicleStats
)
from app.services.roi_service import roi_service
from app.services.stream_service import stream_service
from app.services.model_service import model_service

router = APIRouter(prefix="/api/v1", tags=["detection"])


# ── ROI ───────────────────────────────────────────────────────────────────────

@router.post("/roi", response_model=SuccessResponse)
async def set_roi(body: RoiRequest):
    # Back-compat: default slot = primary
    roi_service.set_roi(body.points, body.active, slot="primary")
    return SuccessResponse(
        success=True,
        message=f"ROI set: {len(body.points)} points, active={roi_service.active}",
    )

@router.post("/roi/{slot}", response_model=SuccessResponse)
async def set_roi_slot(slot: str, body: RoiRequest):
    roi_service.set_roi(body.points, body.active, slot=slot)
    return SuccessResponse(
        success=True,
        message=f"ROI[{slot}] set: {len(body.points)} points, active={roi_service.active_for(slot)}",
    )


@router.delete("/roi", response_model=SuccessResponse)
async def clear_roi():
    for slot in ("primary", "companion", "2", "3"):
        roi_service.clear(slot=slot)
    return SuccessResponse(success=True, message="ROI cleared (all slots)")

@router.delete("/roi/{slot}", response_model=SuccessResponse)
async def clear_roi_slot(slot: str):
    roi_service.clear(slot=slot)
    return SuccessResponse(success=True, message=f"ROI[{slot}] cleared")


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats():
    s = stream_service.stats
    s.model_loaded = model_service.is_loaded
    s.model_name = model_service.name
    # Merged totals + per-camera breakdown for dashboard
    payload = s.model_dump()
    payload["by_slot"] = stream_service.stats_by_slot()
    return payload


@router.post("/stats/reset", response_model=SuccessResponse)
async def reset_stats():
    stream_service.reset_counters()
    return SuccessResponse(success=True, message="Counters reset")


@router.get("/timeline")
async def get_timeline():
    return {"timeline": stream_service.timeline}


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings():
    cong = stream_service.congestion_state
    return {
        "conf_threshold": stream_service.conf_threshold,
        "line_position": stream_service.line_position,
        "max_fps": stream_service.max_fps,
        "tracker_type": stream_service._tracker_type,
        "counting_mode": stream_service._counter.mode,
        "congestion_threshold": cong.threshold,
        "congestion_duration": cong.stable_duration,
        "jpeg_quality": stream_service.jpeg_quality,
        "max_width": stream_service.max_width,
        "skip_frames": stream_service.skip_frames,
    }


@router.patch("/settings", response_model=SuccessResponse)
async def update_settings(body: SettingsUpdate):
    stream_service.update_settings(
        conf=body.conf_threshold,
        line=body.line_position,
        fps=body.max_fps,
        tracker_type=body.tracker_type,
        counting_mode=body.counting_mode,
        congestion_threshold=body.congestion_threshold,
        congestion_duration=body.congestion_duration,
        jpeg_quality=body.jpeg_quality,
        max_width=body.max_width,
        skip_frames=body.skip_frames,
    )
    return SuccessResponse(success=True, message="Settings updated")
