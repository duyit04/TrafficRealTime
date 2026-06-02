"""
Pydantic models for Traffic Light Optimization API.
"""

from __future__ import annotations
from pydantic import BaseModel, Field


class ATCSConfigPublic(BaseModel):
    """Runtime ATCS tuning (defaults from env; PATCH updates until process restart restores from .env baseline on cold start — see service which re-reads Settings at init only)."""
    stop_speed_px_s: float = Field(8.0, ge=0.5, le=200)
    min_stopped_frames: int = Field(5, ge=1, le=120)
    gap_clear_seconds: float = Field(3.0, ge=0.25, le=60)
    extension_step_seconds: float = Field(4.0, ge=0.5, le=30)
    near_end_extend_seconds: float = Field(4.0, ge=0.5, le=60)
    extension_min_approaching: int = Field(2, ge=1, le=50)
    yellow_seconds: float = Field(3.0, ge=1.0, le=15)
    all_red_seconds: float = Field(1.5, ge=0.0, le=10)
    min_green_seconds: float = Field(10.0, ge=3.0, le=180)
    max_green_seconds: float = Field(70.0, ge=10.0, le=300)
    max_red_wait_seconds: float = Field(90.0, ge=15.0, le=900)
    wait_penalty_max_seconds: float = Field(15.0, ge=0.0, le=180)
    gap_out_requires_opposite_queue: bool = False
    gap_out_requires_zero_approaching: bool = True
    # Half-band around counting line as fraction of frame height (0 = disable intersection occupancy)
    intersection_half_band_frac: float = Field(0.035, ge=0.0, le=0.25)
    gap_out_requires_intersection_clear: bool = True
    # After clearance: skip empty scheduled green if the other approach has vehicles (queue or moving)
    actuated_prefer_demand_phase: bool = True
    priority_wait_fraction: float = Field(0.67, ge=0.1, le=0.98)


class ATCSConfigPatch(BaseModel):
    stop_speed_px_s: float | None = Field(None, ge=0.5, le=200)
    min_stopped_frames: int | None = Field(None, ge=1, le=120)
    gap_clear_seconds: float | None = Field(None, ge=0.25, le=60)
    extension_step_seconds: float | None = Field(None, ge=0.5, le=30)
    near_end_extend_seconds: float | None = Field(None, ge=0.5, le=60)
    extension_min_approaching: int | None = Field(None, ge=1, le=50)
    yellow_seconds: float | None = Field(None, ge=1.0, le=15)
    all_red_seconds: float | None = Field(None, ge=0.0, le=10)
    min_green_seconds: float | None = Field(None, ge=3.0, le=180)
    max_green_seconds: float | None = Field(None, ge=10.0, le=300)
    max_red_wait_seconds: float | None = Field(None, ge=15.0, le=900)
    wait_penalty_max_seconds: float | None = Field(None, ge=0.0, le=180)
    gap_out_requires_opposite_queue: bool | None = None
    gap_out_requires_zero_approaching: bool | None = None
    intersection_half_band_frac: float | None = Field(None, ge=0.0, le=0.25)
    gap_out_requires_intersection_clear: bool | None = None
    actuated_prefer_demand_phase: bool | None = None
    priority_wait_fraction: float | None = Field(None, ge=0.1, le=0.98)


class InferredApproachSnapshot(BaseModel):
    """Behavior-inferred signal state for one approach (upper=0 / lower=1)."""
    label: str = "UNKNOWN"  # GREEN | RED | YELLOW | UNKNOWN
    confidence: float = 0.0
    avg_speed: float = 0.0
    moving_ratio: float = 0.0
    crossing_rate: float = 0.0
    stopped_count: int = 0
    total_count: int = 0
    front_speed: float = 0.0
    # ── Ngữ cảnh “đỏ” — xe dừng trong vùng hành vi (proxy ROI quanh vạch, không nhận pixel đèn đỏ) ──
    when_red_roi_stopped: int = 0           # chỉ có ý nghĩa khi label ~ RED
    when_red_hint_next_green_sec: float = 0.0   # Fuzzy → thời lượng xanh nên giành khi tới phiên hướng này
    when_red_wait_budget_hint_sec: float = 0.0  # Gợi ý độ chờ trong chu kỳ (chuẩn hóa từ hàng chờ + thời chờ TLC)


class InferredIntersectionSnapshot(BaseModel):
    """Inverse detector output + optional disagreement with controller."""
    enabled: bool = False
    approach0: InferredApproachSnapshot = Field(default_factory=InferredApproachSnapshot)
    approach1: InferredApproachSnapshot = Field(default_factory=InferredApproachSnapshot)
    mismatch: bool = False
    mismatch_hint: str = ""


class LaneDensityAdvice(BaseModel):
    """
    Goi y giay xanh tu fuzzy theo hai hang (hai pha): luong chinh + optional companion RTSP.
    Khi cấu hình TLC_SIGNAL_BBOX — chỉ 'active' sau khi thấy pixel đỏ ổn định trong crop.
    """
    enabled: bool = False
    visual_gate_active: bool = False
    visual_red_active: bool = False
    visual_red_ratio: float = 0.0
    companion_lane_active: bool = False
    stopped_phase0: int = 0
    stopped_phase1: int = 0
    suggest_green_phase0_sec: float = 0.0
    suggest_green_phase1_sec: float = 0.0
    suggest_cycle_total_sec: float = 0.0
    note: str = ""


class PhaseHistoryEvent(BaseModel):
    """Audit event for traffic light state transitions."""
    ts: float
    event: str
    phase: int | None = None
    reason: str = ""
    queue: list[int] = Field(default_factory=lambda: [0, 0])
    wait: list[float] = Field(default_factory=lambda: [0.0, 0.0])
    mode: str = "fuzzy"
    intersection_state: str = "green"


class TrafficLightPhase(BaseModel):
    """Current state of one traffic light phase."""
    phase_id: int = 0
    color: str = "red"                  # "red" | "yellow" | "green"
    # Đồng hồ gợi ý (giảm dần): đỏ → green_time (G rồi vàng); xanh/vàng → red_time_hint (R)
    green_time: float = 30.0
    red_time_hint: float = 0.0
    # Loại gợi ý đang đếm (UI): green | yellow | red
    advice_countdown: str = ""
    # Giây gợi ý cố định (nhãn cạnh chấm tròn — không đếm ngược)
    advice_peak_sec: float = 0.0
    queue_length: int = 0               # stopped / waiting vehicles (for TLC decisions)
    approaching_count: int = 0        # moving vehicles in approach (extension heuristic)
    avg_wait: float = 0.0               # max observed wait on stopped tracks (s)


class TrafficLightState(BaseModel):
    """Full traffic light intersection state."""
    phases: list[TrafficLightPhase] = Field(default_factory=lambda: [
        TrafficLightPhase(phase_id=0, color="red"),
        TrafficLightPhase(phase_id=1, color="red"),
    ])
    active_phase: int = 0
    cycle_count: int = 0
    mode: str = "manual"                # "manual" | "fuzzy" | "rl" | "auto"
    # True while a camera stream worker is feeding the TLC (otherwise UI stays idle)
    stream_attached: bool = False
    # Intersection sub-state: green / yellow / all_red (clearance)
    intersection_state: str = "green"
    # Which approach shows yellow (None when not in yellow clearance)
    yellow_phase_id: int | None = None
    # Short UI hint: gap_out | priority_wait | forced_switch | ""
    ui_hint: str = ""
    # Behavior-based inverse inference (no camera on traffic heads)
    inferred: InferredIntersectionSnapshot = Field(default_factory=InferredIntersectionSnapshot)
    lane_density_advice: LaneDensityAdvice = Field(default_factory=LaneDensityAdvice)


class FuzzyDecision(BaseModel):
    """Result of fuzzy inference."""
    green_time: float
    queue_input: float
    wait_input: float
    approaching_input: float | None = None
    firing_rules: list[str] = []


class RLStatus(BaseModel):
    """RL agent training status."""
    states_visited: int = 0
    total_steps: int = 0
    total_episodes: int = 0
    epsilon: float = 0.15
    avg_reward: float = 0.0
    avg_q: float = 0.0
    max_q: float = 0.0


class GAStatus(BaseModel):
    """GA optimization status."""
    running: bool = False
    generation: int = 0
    total_generations: int = 0
    best_fitness: float = 0.0
    avg_fitness: float = 0.0
    history: list[dict] = []


class SimulationRequest(BaseModel):
    """Request to run a traffic simulation."""
    mode: str = "fuzzy"                 # "fuzzy" | "rl" | "fixed"
    episodes: int = 10
    cycles_per_episode: int = 100
    arrival_rate_0: float = 0.3
    arrival_rate_1: float = 0.25
    fixed_green: float = 30.0           # used when mode="fixed"


class SimulationResult(BaseModel):
    """Result of simulation run."""
    mode: str
    episodes: int
    avg_reward: float
    avg_wait: float
    total_cleared: int
    avg_queue: float


class GARequest(BaseModel):
    """Request to start GA optimization."""
    population_size: int = 50
    generations: int = 30
    eval_episodes: int = 5
    eval_cycles: int = 50


class TrafficLightConfig(BaseModel):
    """Configuration update for traffic light system."""
    mode: str | None = None             # "manual" | "fuzzy" | "rl" | "auto"
    arrival_rate_0: float | None = None
    arrival_rate_1: float | None = None
    min_green: float | None = None
    max_green: float | None = None
    rl_alpha: float | None = None
    rl_gamma: float | None = None
    rl_epsilon: float | None = None


class TrafficLightSourceAssign(BaseModel):
    """
    Map UI-selected cameras to the 2 traffic-light phases.
    slot can be: "primary", "companion", "2", "3" (extra live slots).
    """
    phase0_slot: str = "primary"
    phase1_slot: str = "companion"
