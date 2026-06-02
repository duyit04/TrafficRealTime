/**
 * API Service – all REST calls to FastAPI backend.
 * Base URL proxied via Vite dev server to http://localhost:8000
 */

import type {
  ModelInfo,
  RoiRequest,
  Settings,
  StreamStartRequest,
  SuccessResponse,
  VehicleStats,
  FramePayload,
  CompanionFramePayload,
} from '../types/detection';

const BASE = '/api/v1';

/**
 * Build a WebSocket URL that works in both dev (Vite proxy) and production.
 * In dev, Vite proxies /ws → ws://localhost:8000/ws so we just use relative path.
 */
export function getWebSocketUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host; // includes port in dev (e.g. localhost:5173)
  return `${proto}//${host}${path}`;
}

async function apiFetch<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json().catch(() => {
    throw new Error('Invalid JSON response from server');
  });
}

// ── Models ────────────────────────────────────────────────────────────────────

export const modelApi = {
  list: () => apiFetch<ModelInfo[]>('/models'),

  upload: async (file: File): Promise<SuccessResponse> => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${BASE}/models/upload`, { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Upload failed');
    }
    return res.json().catch(() => {
      throw new Error('Invalid JSON response from server');
    });
  },

  load: (name: string) =>
    apiFetch<SuccessResponse>('/models/load', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),

  delete: (name: string) =>
    apiFetch<SuccessResponse>(`/models/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),

  /** Bắt đầu export .pt → TensorRT .engine (chạy nền trên server). */
  exportEngine: (body: {
    name: string;
    fp16?: boolean | null;
    workspace_gb?: number | null;
    imgsz?: number | null;
    load_after_export?: boolean;
  }) =>
    apiFetch<SuccessResponse>('/models/export-engine', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  getExportEngineStatus: () =>
    apiFetch<{
      running: boolean;
      done: boolean;
      ok: boolean;
      error: string | null;
      model: string | null;
      engine: string | null;
      started_at: number | null;
      ended_at: number | null;
      /** 0–100, ước lượng từ backend trong lúc build */
      progress?: number;
      progress_message?: string | null;
    }>('/models/export-engine/status'),
};

// ── Stream ────────────────────────────────────────────────────────────────────

export interface StreamStatus {
  active: boolean;
  fps: number;
  frame_count: number;
  error: string | null;
}

export interface DeviceInfo {
  cuda_available: boolean;
  device_name: string | null;
}

export const streamApi = {
  start: (body: StreamStartRequest) =>
    apiFetch<SuccessResponse>('/stream/start', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  stop: () => apiFetch<SuccessResponse>('/stream/stop', { method: 'POST' }),

  startCompanion: (url: string) =>
    apiFetch<SuccessResponse>(`/stream/companion/start?url=${encodeURIComponent(url)}`, { method: 'POST' }),

  stopCompanion: () =>
    apiFetch<SuccessResponse>('/stream/companion/stop', { method: 'POST' }),

  startExtra: (slot: number, url: string) =>
    apiFetch<SuccessResponse>(`/stream/extra/start?slot=${slot}&url=${encodeURIComponent(url)}`, { method: 'POST' }),

  stopExtra: (slot: number) =>
    apiFetch<SuccessResponse>(`/stream/extra/stop?slot=${slot}`, { method: 'POST' }),

  getStatus: () => apiFetch<StreamStatus>('/stream/status'),

  getFrame: () => apiFetch<FramePayload>('/stream/frame'),

  getCompanionFrame: () => apiFetch<CompanionFramePayload>('/stream/companion/frame'),

  getDevice: () => apiFetch<DeviceInfo>('/stream/device'),
};

// ── Detection / ROI / Stats ───────────────────────────────────────────────────

export const detectionApi = {
  setRoi: (body: RoiRequest) =>
    apiFetch<SuccessResponse>('/roi', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  setRoiSlot: (slot: string | number, body: RoiRequest) =>
    apiFetch<SuccessResponse>(`/roi/${encodeURIComponent(String(slot))}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  clearRoi: () => apiFetch<SuccessResponse>('/roi', { method: 'DELETE' }),

  clearRoiSlot: (slot: string | number) =>
    apiFetch<SuccessResponse>(`/roi/${encodeURIComponent(String(slot))}`, { method: 'DELETE' }),

  getStats: () => apiFetch<VehicleStats>('/stats'),

  resetStats: () => apiFetch<SuccessResponse>('/stats/reset', { method: 'POST' }),

  getTimeline: () => apiFetch<{ timeline: Array<{ t: number; v: number }> }>('/timeline'),

  getSettings: () => apiFetch<Settings>('/settings'),

  updateSettings: (body: Partial<Settings>) =>
    apiFetch<SuccessResponse>('/settings', {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
};

// ── Traffic Light (display-only) ────────────────────────────────────────────

export interface TLPhase {
  phase_id: number;
  color: 'red' | 'yellow' | 'green';
  /** Gợi ý xanh còn lại (pha đỏ) — backend đếm ngược */
  green_time?: number;
  /** Gợi ý khối đỏ còn lại (pha xanh/vàng) — backend đếm ngược */
  red_time_hint?: number;
  /** Loại gợi ý đang đếm: green | yellow | red */
  advice_countdown?: '' | 'green' | 'yellow' | 'red';
  /** Giây gợi ý cố định (nhãn cạnh chấm tròn, không đếm ngược) */
  advice_peak_sec?: number;
  queue_length?: number;
}

export interface TLState {
  phases: TLPhase[];
  active_phase: number;
  cycle_count?: number;
  stream_attached?: boolean;
  intersection_state?: 'green' | 'yellow' | 'all_red';
  yellow_phase_id?: number | null;
  lane_density_advice?: { enabled?: boolean; note?: string };
}

// ── Media (image detect + video upload) ──────────────────────────────────────

export interface ImageDetectResult {
  success: boolean;
  image: string;
  width: number;
  height: number;
  detections: Array<{
    x1: number; y1: number; x2: number; y2: number;
    class_name: string; confidence: number; track_id?: number;
  }>;
  count: number;
}

export const mediaApi = {
  detectImage: async (file: File): Promise<ImageDetectResult> => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${BASE}/media/detect-image`, { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Detection failed');
    }
    return res.json().catch(() => {
      throw new Error('Invalid JSON response from server');
    });
  },

  startVideo: async (file: File): Promise<{ success: boolean; filename: string; size: number }> => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${BASE}/media/start-video`, { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Upload failed');
    }
    return res.json().catch(() => {
      throw new Error('Invalid JSON response from server');
    });
  },

  stopVideo: () => apiFetch<SuccessResponse>('/media/stop-video', { method: 'POST' }),

  getStatus: () =>
    apiFetch<{ source_mode: string; running: boolean; stream_active: boolean }>('/media/status'),
};

export const trafficLightApi = {
  getState: () => apiFetch<TLState>('/traffic-light/state'),
  setSources: (body: { phase0_slot: string; phase1_slot: string }) =>
    apiFetch<TLState>('/traffic-light/sources', { method: 'POST', body: JSON.stringify(body) }),
  setAdviceEnabled: (enabled: boolean) =>
    apiFetch<TLState>(`/traffic-light/advice?enabled=${enabled ? 'true' : 'false'}`, { method: 'POST' }),
};

