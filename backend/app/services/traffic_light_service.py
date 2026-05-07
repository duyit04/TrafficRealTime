"""Traffic Light Service (display-only).

This project now keeps *only* a fixed-time traffic-light display cycle:
green → yellow → all_red → switch phase.

All previous control / optimization algorithms (Fuzzy/RL/GA, behavior inference,
lane-density advice, decide/advance/train/simulate...) have been removed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.logger import logger
from app.models.traffic_light_model import TrafficLightState


def _normalize_slot(slot: str) -> str:
    s = (slot or "").strip().lower()
    if s in ("primary", "0", "main", "cam1", "camera1"):
        return "primary"
    if s in ("companion", "1", "cam2", "camera2"):
        return "companion"
    if s in ("2", "3"):
        return s
    if s.startswith("slot"):
        tail = s[4:].strip()
        if tail in ("2", "3"):
            return tail
    raise ValueError("Invalid slot. Use primary|companion|2|3.")


@dataclass
class LaneObs:
    stopped_count: int = 0
    total_count: int = 0
    updated_at: float = 0.0


class DisplayOnlyTrafficLightService:
    def __init__(self) -> None:
        self._state = TrafficLightState()
        self._state.mode = "manual"

        self._state_lock = threading.RLock()
        self._stream_live: bool = False

        # Fixed-cycle timing for UI display.
        # Keep green fixed at 30s (historical requirement).
        self._green_seconds: float = 30.0
        self._phase_green_seconds = [30.0, 30.0]
        self._yellow_seconds: float = float(settings.TLC_YELLOW_SECONDS)
        self._all_red_seconds: float = float(settings.TLC_ALL_RED_SECONDS)

        # Sub-state: green → yellow → all_red
        self._movement_substate: str = "green"
        self._active_phase: int = 0
        self._pending_next_green: int = 1  # alternates between phase 0 and 1

        self._cycle_count: int = 0
        self._phase_started_at: float = time.monotonic()

        self._ticker_thread: threading.Thread | None = None
        self._ticker_running: bool = True

        # Phase ↔ camera slot mapping (which stream provides behavior for each approach)
        self._phase_slot = ["primary", "companion"]
        self._obs_by_slot: dict[str, LaneObs] = {}
        self._start_ticker()
        with self._state_lock:
            self._sync_state_locked()

    # ── Public API ──────────────────────────────────────────────────────────
    def set_stream_live(self, attached: bool) -> None:
        with self._state_lock:
            self._stream_live = bool(attached)
            self._state.stream_attached = self._stream_live

            if not self._stream_live:
                # Idle: both heads red; keep stable state for UI.
                self._movement_substate = "green"
                self._active_phase = 0
                self._pending_next_green = 1
                self._cycle_count = 0
                self._phase_started_at = time.monotonic()
                self._sync_state_locked()
                return

            # Start cycle from green on attach.
            self._cycle_count = 0
            self._active_phase = 0
            self._pending_next_green = 1
            self._movement_substate = "green"
            self._phase_started_at = time.monotonic()
            self._sync_state_locked()

    def get_state(self) -> TrafficLightState:
        with self._state_lock:
            # Pydantic v2 deep copy to avoid UI mutation races.
            return self._state.model_copy(deep=True)

    def set_sources(self, phase0_slot: str, phase1_slot: str) -> None:
        with self._state_lock:
            self._phase_slot[0] = _normalize_slot(phase0_slot)
            self._phase_slot[1] = _normalize_slot(phase1_slot)
            self._sync_state_locked()

    def update_lane_observation(self, slot: str, stopped_count: int, total_count: int) -> None:
        """
        Called by stream workers (per camera) to feed behavior into TLC UI.
        """
        key = _normalize_slot(slot)
        now = time.monotonic()
        obs = self._obs_by_slot.get(key) or LaneObs()
        obs.stopped_count = int(max(0, stopped_count))
        obs.total_count = int(max(0, total_count))
        obs.updated_at = float(now)
        self._obs_by_slot[key] = obs

    # ── Internal loop ───────────────────────────────────────────────────────
    def _start_ticker(self) -> None:
        if self._ticker_thread and self._ticker_thread.is_alive():
            return

        def _loop() -> None:
            tick_s = 0.25
            while self._ticker_running:
                try:
                    with self._state_lock:
                        now = time.monotonic()
                        elapsed = now - self._phase_started_at

                        if self._stream_live:
                            if self._movement_substate == "green":
                                if elapsed >= self._green_seconds:
                                    self._movement_substate = "yellow"
                                    self._phase_started_at = now
                                    # leaving green for the current active phase
                                    self._state.yellow_phase_id = int(self._active_phase)

                            elif self._movement_substate == "yellow":
                                if elapsed >= self._yellow_seconds:
                                    self._movement_substate = "all_red"
                                    self._phase_started_at = now
                                    self._state.yellow_phase_id = None

                            elif self._movement_substate == "all_red":
                                if elapsed >= self._all_red_seconds:
                                    self._cycle_count += 1
                                    self._active_phase = int(self._pending_next_green)
                                    self._pending_next_green = 1 - self._active_phase
                                    self._movement_substate = "green"
                                    self._phase_started_at = now
                                    # Apply per-phase suggested green when entering green
                                    try:
                                        self._green_seconds = float(self._phase_green_seconds[self._active_phase])
                                    except Exception:
                                        self._green_seconds = 30.0

                        self._sync_state_locked()

                except Exception as e:
                    logger.debug("DisplayOnlyTrafficLightService ticker error: %s", e)

                time.sleep(tick_s)

        self._ticker_thread = threading.Thread(target=_loop, daemon=True)
        self._ticker_thread.start()

    def _sync_state_locked(self) -> None:
        now_elapsed = time.monotonic() - self._phase_started_at
        self._state.time_elapsed = float(now_elapsed) if self._stream_live else 0.0
        self._state.cycle_count = int(self._cycle_count)
        self._state.active_phase = int(self._active_phase)
        self._state.stream_attached = bool(self._stream_live)

        # Ensure both phases exist (expected 2 heads).
        for idx, p in enumerate(self._state.phases[:2]):
            p.phase_id = idx
            slot = self._phase_slot[idx] if idx < len(self._phase_slot) else "primary"
            obs = self._obs_by_slot.get(slot)
            # Consider obs stale after 2 seconds
            stale = True
            if obs is not None:
                stale = (time.monotonic() - float(obs.updated_at)) > 2.0
            p.queue_length = 0 if (obs is None or stale) else int(obs.stopped_count)
            p.approaching_count = 0
            p.avg_wait = 0.0

            # Suggest green-time based on stopped queue in ROI for this phase.
            base = float(settings.TLC_MIN_GREEN)
            cap = float(settings.TLC_MAX_GREEN)
            coeff = float(getattr(settings, "TLC_STOPPED_GREEN_COEFF", 2.5) or 2.5)
            suggest = base + coeff * float(p.queue_length)
            suggest = max(base, min(cap, suggest))
            self._phase_green_seconds[idx] = float(suggest)
            p.green_time = float(suggest)

        if not self._stream_live:
            self._state.intersection_state = "green"
            self._state.yellow_phase_id = None
            for p in self._state.phases[:2]:
                p.color = "red"
                p.remaining = 0.0
            self._state.ui_hint = ""
            return

        elapsed = max(0.0, float(now_elapsed))

        if self._movement_substate == "green":
            self._state.intersection_state = "green"
            self._state.yellow_phase_id = None
            rem = max(0.0, self._green_seconds - elapsed)
            for idx, p in enumerate(self._state.phases[:2]):
                if idx == self._active_phase:
                    p.color = "green"
                    p.remaining = float(rem)
                else:
                    p.color = "red"
                    p.remaining = 0.0

        elif self._movement_substate == "yellow":
            self._state.intersection_state = "yellow"
            # yellow_phase_id is set in ticker loop when entering "yellow".
            rem = max(0.0, self._yellow_seconds - elapsed)
            for idx, p in enumerate(self._state.phases[:2]):
                if idx == self._active_phase:
                    p.color = "yellow"
                    p.remaining = float(rem)
                else:
                    p.color = "red"
                    p.remaining = 0.0

        else:  # all_red
            self._state.intersection_state = "all_red"
            self._state.yellow_phase_id = None
            for p in self._state.phases[:2]:
                p.color = "red"
                p.remaining = 0.0

        self._state.ui_hint = ""


# Singleton used across the backend.
traffic_light_service = DisplayOnlyTrafficLightService()

