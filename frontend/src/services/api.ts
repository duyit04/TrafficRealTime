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

async function apiFetch<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json();
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
    return res.json();
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

  clearRoi: () => apiFetch<SuccessResponse>('/roi', { method: 'DELETE' }),

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
  remaining: number;
  green_time?: number;
}

export interface TLState {
  phases: TLPhase[];
  active_phase: number;
  cycle_count?: number;
  stream_attached?: boolean;
  intersection_state?: 'green' | 'yellow' | 'all_red';
  yellow_phase_id?: number | null;
}

export const trafficLightApi = {
  getState: () => apiFetch<TLState>('/traffic-light/state'),
};

