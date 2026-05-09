"""Traffic Light API routes (display-only).

This project previously included Fuzzy/RL/GA control and suggestion endpoints.
Those have been removed/disabled; only `/state` remains for UI display.
"""

from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Query

from app.models.traffic_light_model import TrafficLightState, TrafficLightSourceAssign
from app.services.traffic_light_service import traffic_light_service as tls

router = APIRouter(prefix="/api/v1/traffic-light", tags=["traffic-light"])


@router.get("/state", response_model=TrafficLightState)
async def get_state():
    return tls.get_state()


@router.post("/sources", response_model=TrafficLightState)
async def set_sources(body: TrafficLightSourceAssign):
    tls.set_sources(body.phase0_slot, body.phase1_slot)
    return tls.get_state()


@router.post("/advice", response_model=TrafficLightState)
async def set_advice_enabled(
    enabled: Annotated[bool, Query(description="Enable lane-density green-time suggestions")] = True,
):
    tls.set_advice_enabled(bool(enabled))
    return tls.get_state()
