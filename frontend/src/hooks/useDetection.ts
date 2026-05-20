/**
 * useDetection – stream state and actions.
 * Video: H264 WebSocket only. This hook receives stats/detections via JSON WS (no JPEG).
 */

import { useState, useEffect, useCallback, useRef, useMemo, type MutableRefObject } from 'react';
import { detectionApi, streamApi, getWebSocketUrl } from '../services/api';
import { useWebSocket } from './useWebSocket';
import type { Detection, FramePayload, VehicleStats, Settings } from '../types/detection';

const DEFAULT_STATS: VehicleStats = {
  total: 0,
  count_in: 0,
  count_out: 0,
  classes: {},
  classes_in: {},
  classes_out: {},
  counting_mode: 'all',
  fps: 0,
  fps_capture: 0,
  fps_inference: 0,
  fps_sent: 0,
  avg_inference_ms: 0,
  frame_count: 0,
  stream_active: false,
  model_loaded: false,
  model_name: '',
  roi_active: false,
  conf_threshold: 0.35,
  line_position: 0.55,
  congestion: {
    is_congested: false,
    vehicle_count: 0,
    threshold: 10,
    duration_seconds: 0,
    stable_duration: 5,
    message: '',
    level: 'normal',
  },
};

const WS_URL = getWebSocketUrl('/ws/stream');
const WS_COMPANION_URL = getWebSocketUrl('/ws/companion');
const DETECTION_HOLD_MS = 120;

function zeroLiveThroughput(stats: VehicleStats): VehicleStats {
  return {
    ...stats,
    fps: 0,
    fps_capture: 0,
    fps_inference: 0,
    fps_sent: 0,
    frame_count: 0,
    avg_inference_ms: 0,
  };
}

function applyDetections(
  next: Detection[],
  lastRef: MutableRefObject<{ ts: number; dets: Detection[] }>,
  setDets: (d: Detection[]) => void,
) {
  const now = Date.now();
  if (next.length > 0) {
    lastRef.current = { ts: now, dets: next };
    setDets(next);
  } else {
    const age = now - (lastRef.current.ts || 0);
    if (age <= DETECTION_HOLD_MS) {
      setDets(lastRef.current.dets);
    } else {
      setDets([]);
    }
  }
}

export function useDetection() {
  const [detections, setDetections] = useState<Detection[]>([]);
  const [stats, setStats] = useState<VehicleStats>(DEFAULT_STATS);
  const [streamActive, setStreamActive] = useState(false);
  const [companionActive, setCompanionActive] = useState(false);
  const [companionStreamActive, setCompanionStreamActive] = useState(false);
  const [companionDetections, setCompanionDetections] = useState<Detection[]>([]);
  const [companionFps, setCompanionFps] = useState(0);
  const [companionLinePosition, setCompanionLinePosition] = useState<number>(DEFAULT_STATS.line_position);
  const [extraLive, setExtraLive] = useState<
    Record<number, { dets: Detection[]; fps: number; active: boolean }>
  >({});
  const settingsTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const lastDetectionsRef = useRef<{ ts: number; dets: Detection[] }>({ ts: 0, dets: [] });
  const lastCompanionDetsRef = useRef<{ ts: number; dets: Detection[] }>({ ts: 0, dets: [] });

  useEffect(() => {
    detectionApi.getStats().then(setStats).catch(() => {});
  }, []);

  const reloadStats = useCallback(async () => {
    try {
      const s = await detectionApi.getStats();
      setStats(s);
    } catch {
      // ignore
    }
  }, []);

  const handleWsMessage = useCallback((data: unknown) => {
    const payload = data as FramePayload & { slot?: number; fps?: number; stream_active?: boolean };
    const slot = typeof payload.slot === 'number' ? payload.slot : 0;
    if (slot >= 2) {
      setExtraLive((prev) => ({
        ...prev,
        [slot]: {
          dets: payload.detections ?? [],
          fps: payload.fps ?? 0,
          active: payload.stream_active !== false,
        },
      }));
      return;
    }

    if (payload.stats && !payload.stats.stream_active) {
      setStreamActive(false);
      return;
    }
    if (payload.stats) {
      setStats(payload.stats);
    }
    if (Array.isArray(payload.detections)) {
      applyDetections(payload.detections, lastDetectionsRef, setDetections);
    }
  }, []);

  const { connected: wsConnected } = useWebSocket({
    url: WS_URL,
    enabled: streamActive,
    onMessage: handleWsMessage,
    maxRetries: 8,
    retryDelay: 2000,
  });

  const handleCompanionWs = useCallback((data: unknown) => {
    const msg = data as FramePayload & {
      stream_active?: boolean;
      fps?: number;
      lane_stats?: { line_position?: number };
    };
    if (!msg) return;
    if (msg.stream_active === false) {
      setCompanionStreamActive(false);
      setCompanionDetections([]);
      setCompanionFps(0);
      setCompanionLinePosition(DEFAULT_STATS.line_position);
      return;
    }
    setCompanionStreamActive(true);
    if (Array.isArray(msg.detections)) {
      applyDetections(msg.detections, lastCompanionDetsRef, setCompanionDetections);
    }
    if (typeof msg.fps === 'number') setCompanionFps(msg.fps);
    const laneLine = Number(msg?.lane_stats?.line_position);
    if (Number.isFinite(laneLine) && laneLine >= 0 && laneLine <= 1) {
      setCompanionLinePosition(laneLine);
    }
  }, []);

  const { connected: companionWsConnected } = useWebSocket({
    url: WS_COMPANION_URL,
    enabled: streamActive && companionActive,
    onMessage: handleCompanionWs,
    maxRetries: 8,
    retryDelay: 2000,
  });

  const startStream = useCallback(async (url: string, opts?: { companionUrl?: string }) => {
    await streamApi.start({
      url,
      ...(opts?.companionUrl ? { companion_url: opts.companionUrl } : {}),
    });
    setCompanionActive(Boolean(opts?.companionUrl));
    setStreamActive(true);
  }, []);

  const startCompanion = useCallback(async (url: string) => {
    await streamApi.startCompanion(url);
    setCompanionActive(true);
  }, []);

  const stopCompanion = useCallback(async () => {
    await streamApi.stopCompanion();
    setCompanionActive(false);
    setCompanionStreamActive(false);
  }, []);

  const stopStream = useCallback(async () => {
    await streamApi.stop();
    setStreamActive(false);
    setCompanionActive(false);
    setCompanionStreamActive(false);
    setDetections([]);
    setCompanionDetections([]);
    setExtraLive({});
  }, []);

  const setRoi = useCallback(async (points: number[][]) => {
    await detectionApi.setRoi({ points, active: true });
  }, []);

  const setRoiSlot = useCallback(async (slot: string | number, points: number[][]) => {
    await detectionApi.setRoiSlot(slot, { points, active: true });
  }, []);

  const clearRoi = useCallback(async () => {
    await detectionApi.clearRoi();
  }, []);

  const clearRoiSlot = useCallback(async (slot: string | number) => {
    await detectionApi.clearRoiSlot(slot);
  }, []);

  const resetCount = useCallback(async () => {
    try {
      await detectionApi.resetStats();
      const s = await detectionApi.getStats();
      setStats(s);
    } catch (err) {
      console.error('[useDetection] reset failed:', err);
    }
  }, []);

  const updateSettings = useCallback((patch: Partial<Settings>) => {
    clearTimeout(settingsTimerRef.current);
    settingsTimerRef.current = setTimeout(async () => {
      await detectionApi.updateSettings(patch);
    }, 400);
  }, []);

  const statsForUi = useMemo(
    () => (streamActive ? stats : zeroLiveThroughput(stats)),
    [streamActive, stats],
  );

  return {
    detections,
    stats: statsForUi,
    companionDetections,
    companionFps,
    companionLinePosition,
    extraLive,
    wsConnected,
    companionWsConnected,
    streamActive,
    companionActive,
    companionStreamActive,
    startStream,
    startCompanion,
    stopCompanion,
    stopStream,
    reloadStats,
    setRoi,
    clearRoi,
    setRoiSlot,
    clearRoiSlot,
    resetCount,
    updateSettings,
  };
}
