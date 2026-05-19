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
from app.services.roi_service import roi_service


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
        # Whether to compute and apply green-time suggestions from ROI queue.
        self._advice_enabled: bool = False
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

            # Ai được xanh đầu tiên: giống sau all_red — pha có ROI ít xe dừng hơn (ít tắc = ROW).
            # Không được cứng phase 0 luôn xanh (trước đây gây sai khi chỉ pha 0 có hàng chờ).
            self._cycle_count = 0
            self._pending_next_green = 1
            self._movement_substate = "green"
            self._phase_started_at = time.monotonic()
            q0, q1 = self._effective_queues_locked()
            if self._advice_ready_locked() and self._has_demand_locked(q0, q1):
                must = self._strict_row_phase_for_queues_locked(q0, q1)
                first_green = int(must) if must is not None else self._pick_next_green_after_all_red_locked(q0, q1)
                self._active_phase = int(first_green)
                self._pending_next_green = 1 - self._active_phase
                g = self._coupled_green_duration(
                    waiting_phase=int(1 - self._active_phase),
                    serving_phase=int(self._active_phase),
                    q0=q0,
                    q1=q1,
                )
                self._phase_green_seconds[0] = g
                self._phase_green_seconds[1] = g
            else:
                self._active_phase = 0
                self._pending_next_green = 1
                self._phase_green_seconds[0] = 30.0
                self._phase_green_seconds[1] = 30.0
            self._green_seconds = float(self._phase_green_seconds[self._active_phase])
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

    def set_advice_enabled(self, enabled: bool) -> None:
        with self._state_lock:
            prev = self._advice_enabled
            self._advice_enabled = bool(enabled)
            if prev != self._advice_enabled:
                # Reset cycle so toggling always starts fresh
                self._movement_substate = "green"
                self._active_phase = 0
                self._pending_next_green = 1
                self._phase_started_at = time.monotonic()
                self._green_seconds = 30.0
                self._phase_green_seconds = [30.0, 30.0]
                self._cycle_count = 0
            self._sync_state_locked()

    def advice_enabled(self) -> bool:
        with self._state_lock:
            return bool(self._advice_enabled)

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

    def _effective_queues_locked(self) -> tuple[int, int]:
        """Stopped-in-ROI counts per phase (0 if observation missing or stale >2s)."""
        out: list[int] = []
        for idx in range(2):
            slot = self._phase_slot[idx]
            obs = self._obs_by_slot.get(slot)
            stale = True
            if obs is not None:
                stale = (time.monotonic() - float(obs.updated_at)) > 2.0
            q = 0 if (obs is None or stale) else int(obs.stopped_count)
            out.append(q)
        return out[0], out[1]

    def _advice_ready_locked(self) -> bool:
        if not self._advice_enabled:
            return False
        try:
            return bool(
                roi_service.active_for(self._phase_slot[0]) and roi_service.active_for(self._phase_slot[1])
            )
        except Exception:
            return False

    def _has_demand_locked(self, q0: int, q1: int) -> bool:
        """Đang có ít nhất một phương tiện dừng trong hai ROI."""
        return int(q0) > 0 or int(q1) > 0

    def _strict_row_phase_for_queues_locked(self, q0: int, q1: int) -> int | None:
        """
        Pha nào **phải** đang xanh khi có demand: pha có ít xe dừng ROI hơn.
        Hoà q0==q1 != trường hợp không demand: trả None — không ép đổi giữa pha xanh (tránh nhấp nháy).
        """
        if not (self._advice_ready_locked() and self._has_demand_locked(q0, q1)):
            return None
        if int(q0) < int(q1):
            return 0
        if int(q1) < int(q0):
            return 1
        return None

    def _pick_next_green_after_all_red_locked(self, q0: int, q1: int) -> int:
        """
        Pha được xanh NGAY SAU all_red hiện tại.
        Có ít nhất một ROI có xe và gợi ý readiness: LOW queue wins (ít tắc hơn = được ROW).
        Ngược lại: xen kẽ theo _pending_next_green.
        Tie khi có demand: đổi pha xen kẽ qua tie-break để không kẹt.
        """
        if self._advice_ready_locked() and self._has_demand_locked(q0, q1):
            if int(q1) < int(q0):
                return 1
            if int(q0) < int(q1):
                return 0
            return int(self._pending_next_green)
        return int(self._pending_next_green)

    def _coupled_green_duration(self, waiting_phase: int, serving_phase: int, q0: int, q1: int) -> float:
        """
        Một đồng hồ xanh G cho pha được phục vụ (= thời gian đèn đỏ thuần của pha chờ trong giai đoạn này,
        không kể vàng + all_red).
        G = clamp( B + α·q_waiting − β·q_serving, MIN, MAX )
        """
        ql = [int(q0), int(q1)]
        qw = float(ql[int(waiting_phase)])
        qs = float(ql[int(serving_phase)])
        base = float(getattr(settings, "TLC_ADVICE_DEFAULT_SECONDS", 30.0) or 30.0)
        a = float(getattr(settings, "TLC_STOPPED_GREEN_COEFF", 2.5) or 2.5)
        b = float(getattr(settings, "TLC_ADVICE_CROSS_QUEUE_COEFF", 1.5) or 1.5)
        lo = float(settings.TLC_MIN_GREEN)
        hi = float(settings.TLC_MAX_GREEN)
        return float(max(lo, min(hi, base + a * qw - b * qs)))

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

                        if self._stream_live and self._advice_enabled:
                            if self._movement_substate == "green":
                                q0, q1 = self._effective_queues_locked()
                                advice_ok = self._advice_ready_locked()
                                demand_here = advice_ok and self._has_demand_locked(q0, q1)
                                if demand_here:
                                    must = self._strict_row_phase_for_queues_locked(q0, q1)
                                    if must is not None and int(must) != int(self._active_phase):
                                        # Chỉ reset timer khi pha thực sự chuyển, không reset khi số xe thay đổi
                                        self._active_phase = int(must)
                                        self._pending_next_green = 1 - self._active_phase
                                        self._phase_started_at = now
                                        elapsed = 0.0

                                    srv = int(self._active_phase)
                                    wai = int(1 - srv)
                                    geff = self._coupled_green_duration(wai, srv, q0, q1)
                                    self._phase_green_seconds[0] = geff
                                    self._phase_green_seconds[1] = geff
                                    elapsed_g = max(0.0, now - self._phase_started_at)
                                    hi = float(settings.TLC_MAX_GREEN)
                                    self._green_seconds = float(min(hi, max(elapsed_g, geff)))

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
                                    qz0, qz1 = self._effective_queues_locked()
                                    nh = self._pick_next_green_after_all_red_locked(qz0, qz1)
                                    self._active_phase = int(nh)
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
        self._state.lane_density_advice.enabled = bool(self._advice_enabled)

        advice_ready = self._advice_ready_locked()

        note = ""
        if self._advice_enabled and not advice_ready:
            note = "Cần vẽ ROI cho cả 2 camera đã gán để bật gợi ý."
        self._state.lane_density_advice.note = note

        q0, q1 = self._effective_queues_locked()
        demand_here = bool(advice_ready) and self._has_demand_locked(q0, q1)

        if not advice_ready:
            geff_ui = 30.0
        elif demand_here:
            if self._movement_substate == "green":
                wix = int(1 - self._active_phase)
                six = int(self._active_phase)
                geff_ui = self._coupled_green_duration(wix, six, q0, q1)
            elif self._movement_substate == "yellow":
                wix = int(1 - self._active_phase)
                six = int(self._active_phase)
                geff_ui = self._coupled_green_duration(wix, six, q0, q1)
            else:
                nh = self._pick_next_green_after_all_red_locked(q0, q1)
                wix = int(1 - nh)
                six = int(nh)
                geff_ui = self._coupled_green_duration(wix, six, q0, q1)
        else:
            geff_ui = 30.0

        self._phase_green_seconds[0] = float(geff_ui)
        self._phase_green_seconds[1] = float(geff_ui)

        for idx, p in enumerate(self._state.phases[:2]):
            p.phase_id = idx
            slot = self._phase_slot[idx] if idx < len(self._phase_slot) else "primary"
            obs = self._obs_by_slot.get(slot)
            stale = True
            if obs is not None:
                stale = (time.monotonic() - float(obs.updated_at)) > 2.0
            p.queue_length = 0 if (obs is None or stale) else int(obs.stopped_count)
            p.approaching_count = 0
            p.avg_wait = 0.0
            p.green_time = float(geff_ui)
            p.red_time_hint = 0.0

        if not self._stream_live or not self._advice_enabled:
            self._state.intersection_state = "green"
            self._state.yellow_phase_id = None
            for p in self._state.phases[:2]:
                p.color = "red"
                p.remaining = 0.0
                p.time_until_green = 0.0
            self._state.ui_hint = ""
            return

        elapsed = max(0.0, float(now_elapsed))

        if self._movement_substate == "green":
            self._state.intersection_state = "green"
            self._state.yellow_phase_id = None
            rem = max(0.0, self._green_seconds - elapsed)
            clearance = float(self._yellow_seconds + self._all_red_seconds)
            for idx, p in enumerate(self._state.phases[:2]):
                if idx == self._active_phase:
                    p.color = "green"
                    p.remaining = float(rem)
                    p.time_until_green = 0.0
                else:
                    p.color = "red"
                    p.remaining = 0.0
                    p.time_until_green = float(rem + self._yellow_seconds + self._all_red_seconds)

            # Hướng đang XANH: một G chung ⇒ khối đỏ tinh (trước khi được xanh lại) ≈ geff_ui + vàng/all_red).
            if advice_ready:
                ap = int(self._active_phase)
                self._state.phases[ap].red_time_hint = float(geff_ui) + clearance

        elif self._movement_substate == "yellow":
            self._state.intersection_state = "yellow"
            # yellow_phase_id is set in ticker loop when entering "yellow".
            rem = max(0.0, self._yellow_seconds - elapsed)
            for idx, p in enumerate(self._state.phases[:2]):
                if idx == self._active_phase:
                    p.color = "yellow"
                    p.remaining = float(rem)
                    p.time_until_green = 0.0
                else:
                    p.color = "red"
                    p.remaining = 0.0
                    p.time_until_green = float(rem + self._all_red_seconds)

            if advice_ready:
                ap = int(self._active_phase)
                clearance_y = float(self._yellow_seconds + self._all_red_seconds)
                self._state.phases[ap].red_time_hint = float(geff_ui) + clearance_y

        else:  # all_red
            self._state.intersection_state = "all_red"
            self._state.yellow_phase_id = None
            for p in self._state.phases[:2]:
                p.color = "red"
                p.remaining = 0.0
                p.time_until_green = float(self._all_red_seconds - elapsed)

        self._state.ui_hint = ""


# Singleton used across the backend.
traffic_light_service = DisplayOnlyTrafficLightService()

