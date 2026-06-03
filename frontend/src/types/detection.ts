// ── Detection Types ───────────────────────────────────────────────────────────

export interface BoundingBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface Detection {
  bbox: BoundingBox;
  class_name: string;
  confidence: number;
  track_id?: number;
}

export interface CongestionInfo {
  is_congested: boolean;
  vehicle_count: number;
  threshold: number;
  duration_seconds: number;
  stable_duration: number;
  message: string;
  level: 'normal' | 'warning' | 'critical';
}

/** Per-camera counting snapshot (WebSocket lane_stats / API by_slot). */
export interface LaneStats {
  total: number;
  classes: Record<string, number>;
  line_position: number;
  roi_active: boolean;
  roi_count: number;
  roi_total?: number;           // cumulative vehicles that entered ROI
  roi_classes?: Record<string, number>;  // per-class ROI entry count
  fps?: number;
  congestion?: CongestionInfo;
}

export type StatsSlotKey = 'primary' | 'companion' | '2' | '3';

export interface VehicleStats {
  total: number;
  classes: Record<string, number>;
  fps: number;
  fps_capture: number;
  fps_inference: number;
  fps_sent: number;
  avg_inference_ms: number;
  frame_count: number;
  stream_active: boolean;
  model_loaded: boolean;
  model_name: string;
  roi_active: boolean;
  roi_count: number;               // vehicles currently INSIDE the ROI (live)
  roi_total?: number;              // cumulative vehicles that entered ROI
  roi_classes?: Record<string, number>;  // per-class ROI entry count
  conf_threshold: number;
  line_position: number;
  stream_error?: string;
  congestion: CongestionInfo;
  /** Per-camera breakdown when fetched from GET /stats */
  by_slot?: Partial<Record<StatsSlotKey, LaneStats>>;
}

// ── WebSocket Payload ─────────────────────────────────────────────────────────

export interface FramePayload {
  frame: string | null;    // base64 JPEG (HTTP polling) or null when using binary WS
  frame_blob?: Blob;       // binary WS path: decoded JPEG bytes as Blob
  detections: Detection[];
  stats: VehicleStats;
}

// ── Companion Stream Payload (second RTSP) ────────────────────────────────────

export interface CompanionFramePayload {
  frame: string | null;     // base64 JPEG (null when not available)
  detections: Detection[];
  fps: number;
  frame_count: number;
  stream_active: boolean;
}

// ── API Types ─────────────────────────────────────────────────────────────────

export interface ModelInfo {
  name: string;
  size_mb: number;
  active: boolean;
}

export interface StreamStartRequest {
  url: string;
  /** Second RTSP at same intersection — backend runs light YOLO lane for TLC phase 1. */
  companion_url?: string;
}

export interface RoiPoint {
  x: number;
  y: number;
}

export interface RoiRequest {
  points: number[][];   // [[x,y], ...]
  active: boolean;
}

export interface Settings {
  conf_threshold: number;
  line_position: number;
  max_fps: number;
  tracker_type?: string;  // bytetrack | sort | deepsort
  congestion_threshold?: number;
  congestion_duration?: number;
  jpeg_quality?: number;   // 30–95
  max_width?: number;      // 0 = no resize, otherwise max frame width in px
  skip_frames?: number;    // 0 = no skip, N = run inference every N+1 frames
}

export interface SuccessResponse {
  success: boolean;
  message: string;
}

// ── UI State ──────────────────────────────────────────────────────────────────

export type ToastType = 'success' | 'error' | 'info' | 'warning';

export interface Toast {
  id: number;
  message: string;
  type: ToastType;
}
