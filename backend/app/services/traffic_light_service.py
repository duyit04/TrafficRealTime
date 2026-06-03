"""Traffic Light Service (display-only).

This project now keeps *only* a fixed-time traffic-light display cycle:
green → yellow → switch phase (no all-red clearance).

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
        self._obs_lock = threading.Lock()
        self._stream_live: bool = False

        # Fixed-cycle timing for UI display.
        # Keep green fixed at 30s (historical requirement).
        self._green_seconds: float = 30.0
        self._phase_green_seconds = [30.0, 30.0]
        self._yellow_seconds: float = float(settings.TLC_YELLOW_SECONDS)

        # Sub-state: green → yellow → next green
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

        # Một đồng hồ gợi ý chung / chu kỳ (G rồi vàng) — hai pha luôn khớp, không lệch 30 vs 33.
        self._ui_cycle_armed: bool = False
        self._ui_cycle_anchor_started_at: float = 0.0
        self._ui_cycle_g_sec: float = 30.0
        self._ui_cycle_yellow_sec: float = 3.0
        self._ui_cycle_active_phase: int = -1
        self._ui_prev_movement_substate: str = ""
        self._ui_prev_demand_any: bool = False
        self._ui_post_cycle_rolled: bool = False

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
                self._reset_ui_advice_anchors_locked()
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
            self._reset_ui_advice_anchors_locked()
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
                self._reset_ui_advice_anchors_locked()
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
        with self._obs_lock:
            obs = self._obs_by_slot.get(key) or LaneObs()
            obs.stopped_count = int(max(0, stopped_count))
            obs.total_count = int(max(0, total_count))
            obs.updated_at = float(now)
            self._obs_by_slot[key] = obs

    def _effective_queues_locked(self) -> tuple[int, int]:
        """Stopped-in-ROI counts per phase (0 if observation missing or stale >2s)."""
        out: list[int] = []
        with self._obs_lock:
            snapshot = dict(self._obs_by_slot)
        for idx in range(2):
            slot = self._phase_slot[idx]
            obs = snapshot.get(slot)
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
        Pha được xanh kế tiếp sau khi hết vàng.
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

    def _enter_next_green_after_yellow_locked(self, now: float) -> None:
        """Hết vàng → xanh pha kế ngay (không all_red)."""
        self._cycle_count += 1
        q0, q1 = self._effective_queues_locked()
        nh = self._pick_next_green_after_all_red_locked(q0, q1)
        self._active_phase = int(nh)
        self._pending_next_green = 1 - self._active_phase
        self._movement_substate = "green"
        self._phase_started_at = float(now)
        self._state.yellow_phase_id = None
        advice_ok = self._advice_ready_locked()
        demand_here = advice_ok and self._has_demand_locked(q0, q1)
        if demand_here:
            wix = int(1 - nh)
            geff = self._coupled_green_duration(wix, nh, q0, q1)
        else:
            geff = float(
                getattr(settings, "TLC_ADVICE_NO_DEMAND_SECONDS", 30.0) or 30.0
            )
        self._green_seconds = float(geff)
        self._phase_green_seconds[0] = float(geff)
        self._phase_green_seconds[1] = float(geff)

    def _coupled_green_duration(self, waiting_phase: int, serving_phase: int, q0: int, q1: int) -> float:
        """
        Thời gian xanh G dựa trên số xe dừng ở pha chờ (đỏ):
        G = clamp( base + α·max(0, q_waiting−1), MIN, MAX )
        base = TLC_ADVICE_DEFAULT_SECONDS (thời gian khi q_waiting = 1).
        Mỗi xe thêm ở pha đỏ cộng α giây. Pha xanh luôn ít xe hơn nên không tính.
        """
        ql = [int(q0), int(q1)]
        qw = float(ql[int(waiting_phase)])
        base = float(getattr(settings, "TLC_ADVICE_DEFAULT_SECONDS", 20.0) or 20.0)
        a = float(getattr(settings, "TLC_STOPPED_GREEN_COEFF", 5.0) or 5.0)
        lo = float(settings.TLC_MIN_GREEN)
        hi = float(settings.TLC_MAX_GREEN)
        return float(max(lo, min(hi, base + a * max(0.0, qw - 1.0))))

    def _apply_phase_advice_display_locked(
        self,
        q0: int,
        q1: int,
        advice_ready: bool,
        demand_here: bool,
    ) -> None:
        """
        UI gợi ý: pha đỏ → G rồi vàng; pha xanh/vàng → R = G + vàng (không cộng all_red).
        Để khi hết G, hai bên còn đúng 3s vàng gợi ý (không lệch 4s do all_red).
        """
        default_g = float(getattr(settings, "TLC_ADVICE_NO_DEMAND_SECONDS", 30.0) or 30.0)
        clearance_yellow = float(self._yellow_seconds)

        for p in self._state.phases[:2]:
            p.green_time = default_g
            p.red_time_hint = 0.0

        if not advice_ready:
            return

        def _g(waiting: int, serving: int) -> float:
            if not demand_here:
                return default_g
            return self._coupled_green_duration(int(waiting), int(serving), q0, q1)

        ap = int(self._active_phase)
        sub = self._movement_substate

        if sub in ("green", "yellow"):
            wai = int(1 - ap)
            srv = ap
            g = _g(wai, srv)
            for idx, p in enumerate(self._state.phases[:2]):
                if idx == wai:
                    p.green_time = float(g)
                if idx == srv:
                    p.red_time_hint = float(g) + clearance_yellow

    def _reset_ui_advice_anchors_locked(self) -> None:
        self._ui_cycle_armed = False
        self._ui_cycle_anchor_started_at = 0.0
        self._ui_cycle_g_sec = 30.0
        self._ui_cycle_yellow_sec = float(self._yellow_seconds)
        self._ui_cycle_active_phase = -1
        self._ui_prev_movement_substate = ""
        self._ui_prev_demand_any = False
        self._ui_post_cycle_rolled = False

    def _arm_ui_advice_cycle_locked(self, g_sec: float, active_phase: int, now: float) -> None:
        self._ui_cycle_g_sec = float(max(0.0, g_sec))
        self._ui_cycle_yellow_sec = float(self._yellow_seconds)
        self._ui_cycle_anchor_started_at = float(now)
        self._ui_cycle_active_phase = int(active_phase)
        self._ui_cycle_armed = True
        self._ui_post_cycle_rolled = False

    def _apply_ui_advice_countdown_locked(
        self,
        q0: int,
        q1: int,
        advice_ready: bool,
        demand_here: bool,
    ) -> None:
        """
        Một mốc thời gian từ lúc bắt đầu xanh (ui_cycle_anchor):
        - Pha chờ: G → vàng (cuối chu kỳ gợi ý, không nhảy lại 3s khi đèn mô phỏng vàng)
        - Pha phục vụ: luôn gợi ý R (red_time_hint)
        """
        default_g = float(getattr(settings, "TLC_ADVICE_NO_DEMAND_SECONDS", 30.0) or 30.0)
        now = time.monotonic()
        sub = str(self._movement_substate)
        ap = int(self._active_phase)
        prev_sub = str(self._ui_prev_movement_substate)

        if not advice_ready:
            for p in self._state.phases[:2]:
                p.green_time = default_g
                p.red_time_hint = 0.0
                p.advice_countdown = ""
                p.advice_peak_sec = default_g
            return

        if sub in ("green", "yellow") and self._stream_live and self._advice_enabled:
            wai = int(1 - ap)
            g_nom = (
                self._coupled_green_duration(wai, ap, q0, q1)
                if demand_here
                else default_g
            )

            # Cập nhật g_sec khi xe vào/ra ROI giữa pha (không reset anchor, giữ elapsed)
            if (
                self._ui_cycle_armed
                and sub == "green"
                and self._ui_cycle_active_phase == ap
                and abs(g_nom - float(self._ui_cycle_g_sec)) > 0.5
            ):
                self._ui_cycle_g_sec = float(max(0.0, g_nom))

            need_arm = (
                not self._ui_cycle_armed
                or (sub == "green" and prev_sub == "yellow")
                or self._ui_cycle_active_phase != ap
            )
            if need_arm and sub == "green":
                self._arm_ui_advice_cycle_locked(g_nom, ap, now)

            if self._ui_cycle_armed:
                g = float(self._ui_cycle_g_sec)
                y = float(self._ui_cycle_yellow_sec)
                srv = int(self._ui_cycle_active_phase)
                wai = int(1 - srv)
                total_r = g + y

                # Một đồng hồ duy nhất — không reset khi mô phỏng sang vàng (tránh nhảy lại 3s vàng).
                elapsed = max(
                    0.0, float(now) - float(self._ui_cycle_anchor_started_at)
                )

                # Hết G+vàng gợi ý → bắt chu kỳ kế ngay (không kẹt 0/0).
                if elapsed >= g + y - 1e-6 and not self._ui_post_cycle_rolled:
                    nh = self._pick_next_green_after_all_red_locked(q0, q1)
                    oth = int(1 - nh)
                    g_roll = (
                        self._coupled_green_duration(oth, nh, q0, q1)
                        if demand_here
                        else default_g
                    )
                    self._arm_ui_advice_cycle_locked(g_roll, int(nh), now)
                    self._ui_post_cycle_rolled = True
                    g = float(self._ui_cycle_g_sec)
                    y = float(self._ui_cycle_yellow_sec)
                    srv = int(self._ui_cycle_active_phase)
                    wai = int(1 - srv)
                    total_r = g + y
                    elapsed = 0.0

                y_el = max(0.0, float(now) - float(self._phase_started_at))
                y_rem = max(0.0, float(self._yellow_seconds) - y_el)

                for idx, p in enumerate(self._state.phases[:2]):
                    p.green_time = 0.0
                    p.red_time_hint = 0.0
                    p.advice_countdown = ""

                    if sub == "yellow":
                        if int(idx) == ap:
                            # Đỏ tiếp tục 3→0 (phần cuối của total_r)
                            p.advice_countdown = "red"
                            p.red_time_hint = y_rem
                            # Dùng _green_seconds + yellow thay vì total_r (tránh sai khi roll-over đã fire)
                            p.advice_peak_sec = float(self._green_seconds) + float(self._yellow_seconds)
                        else:
                            # Xanh hết 30s → chạy tiếp 3s vàng
                            p.advice_countdown = "yellow"
                            p.green_time = y_rem
                            p.advice_peak_sec = float(self._yellow_seconds)
                    elif int(idx) == srv:
                        # Đỏ: total_r→0 (33→3 trong xanh, rồi 3→0 trong yellow)
                        p.advice_countdown = "red"
                        p.red_time_hint = float(max(0.0, total_r - elapsed))
                        p.advice_peak_sec = float(total_r)
                    elif int(idx) == wai:
                        # Xanh: g→0 (30→0), về 0 đúng lúc yellow bắt đầu
                        p.advice_countdown = "green"
                        p.green_time = float(max(0.0, g - elapsed))
                        p.advice_peak_sec = float(g)
        self._ui_prev_movement_substate = sub
        self._ui_prev_demand_any = bool(demand_here)

    def _apply_advice_display_colors_locked(self) -> None:
        """
        Trong 3s yellow: giữ màu không đổi (xanh vẫn xanh, đỏ vẫn đỏ).
        Ô đèn chỉ đổi màu khi đồng hồ về 0 (hết 33s), không đổi sớm lúc 3s cuối.
        """
        if not self._advice_enabled or not self._stream_live:
            return
        if str(self._movement_substate) != "yellow":
            return
        ap = int(self._active_phase)
        for idx, p in enumerate(self._state.phases[:2]):
            p.color = "green" if idx == ap else "yellow"

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
                            if self._movement_substate == "all_red":
                                self._enter_next_green_after_yellow_locked(now)
                            elif self._movement_substate == "green":
                                q0, q1 = self._effective_queues_locked()
                                advice_ok = self._advice_ready_locked()
                                demand_here = advice_ok and self._has_demand_locked(q0, q1)
                                no_demand_g = float(
                                    getattr(settings, "TLC_ADVICE_NO_DEMAND_SECONDS", 30.0)
                                    or 30.0
                                )
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
                                else:
                                    self._green_seconds = no_demand_g
                                    self._phase_green_seconds[0] = no_demand_g
                                    self._phase_green_seconds[1] = no_demand_g

                                if elapsed >= self._green_seconds:
                                    self._movement_substate = "yellow"
                                    self._phase_started_at = now
                                    self._state.yellow_phase_id = int(self._active_phase)

                            elif self._movement_substate == "yellow":
                                if elapsed >= self._yellow_seconds:
                                    self._enter_next_green_after_yellow_locked(now)

                        self._sync_state_locked()

                except Exception as e:
                    logger.debug("DisplayOnlyTrafficLightService ticker error: %s", e)

                time.sleep(tick_s)

        self._ticker_thread = threading.Thread(target=_loop, daemon=True)
        self._ticker_thread.start()

    def _sync_state_locked(self) -> None:
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

        if not advice_ready or not demand_here:
            geff_ui = 30.0
        else:
            wix = int(1 - self._active_phase)
            six = int(self._active_phase)
            geff_ui = self._coupled_green_duration(wix, six, q0, q1)

        self._phase_green_seconds[0] = float(geff_ui)
        self._phase_green_seconds[1] = float(geff_ui)

        with self._obs_lock:
            obs_snapshot = dict(self._obs_by_slot)
        for idx, p in enumerate(self._state.phases[:2]):
            p.phase_id = idx
            slot = self._phase_slot[idx] if idx < len(self._phase_slot) else "primary"
            obs = obs_snapshot.get(slot)
            stale = True
            if obs is not None:
                stale = (time.monotonic() - float(obs.updated_at)) > 2.0
            p.queue_length = 0 if (obs is None or stale) else int(obs.stopped_count)
            p.approaching_count = 0
            p.avg_wait = 0.0

        if not self._stream_live or not self._advice_enabled:
            self._state.intersection_state = "green"
            self._state.yellow_phase_id = None
            for p in self._state.phases[:2]:
                p.color = "red"
            self._apply_phase_advice_display_locked(q0, q1, advice_ready, demand_here)
            self._apply_ui_advice_countdown_locked(q0, q1, advice_ready, demand_here)
            self._apply_advice_display_colors_locked()
            self._state.ui_hint = ""
            return

        if self._movement_substate == "green":
            self._state.intersection_state = "green"
            self._state.yellow_phase_id = None
            for idx, p in enumerate(self._state.phases[:2]):
                p.color = "green" if idx == self._active_phase else "red"

        elif self._movement_substate == "yellow":
            self._state.intersection_state = "yellow"
            # yellow_phase_id is set in ticker loop when entering "yellow".
            for idx, p in enumerate(self._state.phases[:2]):
                p.color = "yellow" if idx == self._active_phase else "red"

        else:
            self._state.intersection_state = "green"
            self._state.yellow_phase_id = None
            for idx, p in enumerate(self._state.phases[:2]):
                p.color = "green" if idx == self._active_phase else "red"

        self._apply_phase_advice_display_locked(q0, q1, advice_ready, demand_here)
        self._apply_ui_advice_countdown_locked(q0, q1, advice_ready, demand_here)
        self._apply_advice_display_colors_locked()
        self._state.ui_hint = ""


# Singleton used across the backend.
traffic_light_service = DisplayOnlyTrafficLightService()

