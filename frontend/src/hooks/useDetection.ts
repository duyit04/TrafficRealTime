/**
 * useDetection – stream state and actions.
 * Video: H264 WebSocket only. This hook receives stats/detections via JSON WS (no JPEG).
 */

import { useState, useEffect, useCallback, useRef, useMemo, type MutableRefObject } from 'react';
import { detectionApi, streamApi, getWebSocketUrl } from '../services/api';
import { useWebSocket } from './useWebSocket';
import type {
  Detection,
  FramePayload,
  VehicleStats,
  Settings,
  CongestionInfo,
  LaneStats,
  StatsSlotKey,
} from '../types/detection';

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
  roi_count: 0,
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

function laneStatsToVehicleStats(lane: LaneStats, base: VehicleStats): VehicleStats {
  return {
    ...base,
    total: lane.total ?? 0,
    count_in: lane.count_in ?? 0,
    count_out: lane.count_out ?? 0,
    classes: lane.classes ?? {},
    classes_in: lane.classes_in ?? {},
    classes_out: lane.classes_out ?? {},
    counting_mode: lane.counting_mode ?? base.counting_mode,
    line_position: lane.line_position ?? base.line_position,
    roi_active: lane.roi_active ?? false,
    roi_count: lane.roi_count ?? 0,
    roi_total: lane.roi_total ?? 0,
    roi_classes: lane.roi_classes ?? {},
    fps: lane.fps ?? base.fps,
    congestion: lane.congestion ?? base.congestion,
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
  const [companionCongestion, setCompanionCongestion] = useState<CongestionInfo | null>(null);
  const [companionRoiActive, setCompanionRoiActive] = useState(false);
  const [companionRoiCount, setCompanionRoiCount] = useState(0);
  const [statsBySlot, setStatsBySlot] = useState<Partial<Record<StatsSlotKey, VehicleStats>>>({});
  const [extraLive, setExtraLive] = useState<
    Record<number, { dets: Detection[]; fps: number; active: boolean }>
  >({});
  const settingsTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const lastDetectionsRef = useRef<{ ts: number; dets: Detection[] }>({ ts: 0, dets: [] });
  const lastCompanionDetsRef = useRef<{ ts: number; dets: Detection[] }>({ ts: 0, dets: [] });

  const applyBySlotFromApi = useCallback((s: VehicleStats) => {
    const bySlot = s.by_slot;
    if (!bySlot) return;
    setStatsBySlot((prev) => {
      const next = { ...prev };
      (Object.entries(bySlot) as [StatsSlotKey, LaneStats][]).forEach(([key, lane]) => {
        if (lane) next[key] = laneStatsToVehicleStats(lane, s);
      });
      return next;
    });
  }, []);

  useEffect(() => {
    detectionApi.getStats().then((s) => {
      setStats(s);
      applyBySlotFromApi(s);
    }).catch(() => {});
  }, [applyBySlotFromApi]);

  const reloadStats = useCallback(async () => {
    try {
      const s = await detectionApi.getStats();
      setStats(s);
      applyBySlotFromApi(s);
    } catch {
      // ignore
    }
  }, [applyBySlotFromApi]);

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
      const lane = (payload as { lane_stats?: LaneStats }).lane_stats;
      if (lane) {
        const slotKey = String(slot) as StatsSlotKey;
        setStatsBySlot((prev) => ({
          ...prev,
          [slotKey]: laneStatsToVehicleStats(lane, prev[slotKey] ?? DEFAULT_STATS),
        }));
      }
      return;
    }

    if (payload.stats) {
      setStats(payload.stats);
      setStatsBySlot((prev) => ({
        ...prev,
        primary: payload.stats as VehicleStats,
      }));
      if (!payload.stats.stream_active) {
        setStreamActive(false);
        return;
      }
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
      lane_stats?: {
        line_position?: number;
        congestion?: CongestionInfo;
        roi_active?: boolean;
        roi_count?: number;
      };
    };
    if (!msg) return;
    if (msg.stream_active === false) {
      setCompanionStreamActive(false);
      setCompanionDetections([]);
      setCompanionFps(0);
      setCompanionLinePosition(DEFAULT_STATS.line_position);
      setCompanionCongestion(null);
      setCompanionRoiActive(false);
      setCompanionRoiCount(0);
      setStatsBySlot((prev) => {
        const next = { ...prev };
        delete next.companion;
        return next;
      });
      return;
    }
    setCompanionStreamActive(true);
    if (Array.isArray(msg.detections)) {
      applyDetections(msg.detections, lastCompanionDetsRef, setCompanionDetections);
    }
    if (typeof msg.fps === 'number') setCompanionFps(msg.fps);
    if (msg.lane_stats) {
      const lane = msg.lane_stats as LaneStats;
      setStatsBySlot((prev) => ({
        ...prev,
        companion: laneStatsToVehicleStats(lane, prev.companion ?? DEFAULT_STATS),
      }));
      if (typeof lane.roi_active === 'boolean') {
        setCompanionRoiActive(lane.roi_active);
        setCompanionRoiCount(lane.roi_active ? (lane.roi_count ?? 0) : 0);
      }
      if (lane.congestion) setCompanionCongestion(lane.congestion);
      const laneLine = Number(lane.line_position);
      if (Number.isFinite(laneLine) && laneLine >= 0 && laneLine <= 1) {
        setCompanionLinePosition(laneLine);
      }
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
    setCompanionCongestion(null);
    setCompanionRoiActive(false);
    setCompanionRoiCount(0);
    setStatsBySlot({});
    setExtraLive({});
  }, []);

  /** Bật WS stats sau khi backend đã start (upload video / nguồn không qua startStream). */
  const beginPlayback = useCallback(() => {
    setStreamActive(true);
  }, []);

  const endPlayback = useCallback(() => {
    setStreamActive(false);
    setCompanionActive(false);
    setCompanionStreamActive(false);
    setDetections([]);
    setCompanionDetections([]);
    setStatsBySlot({});
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
      applyBySlotFromApi(s);
      setStatsBySlot((prev) => {
        const cleared = { ...prev };
        (['primary', 'companion', '2', '3'] as StatsSlotKey[]).forEach((k) => {
          if (cleared[k]) {
            cleared[k] = {
              ...cleared[k]!,
              total: 0,
              count_in: 0,
              count_out: 0,
              classes: {},
              classes_in: {},
              classes_out: {},
              roi_total: 0,
              roi_classes: {},
            };
          }
        });
        return cleared;
      });
    } catch (err) {
      console.error('[useDetection] reset failed:', err);
    }
  }, [applyBySlotFromApi]);

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
    companionCongestion,
    companionRoiActive,
    companionRoiCount,
    statsBySlot,
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
    beginPlayback,
    endPlayback,
    reloadStats,
    setRoi,
    clearRoi,
    setRoiSlot,
    clearRoiSlot,
    resetCount,
    updateSettings,
  };
}
