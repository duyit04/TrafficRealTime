/**
 * useDetection – central hook for stream state and actions.
 *
 * Transport strategy:
 *   1. Primary: WebSocket /ws/stream — frames pushed by backend, no per-request overhead
 *   2. Fallback: HTTP polling every 250ms — used when WebSocket fails after maxRetries
 *
 * The hook exposes `wsConnected` and `usingFallback` so the UI can show connection status.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
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
const DETECTION_HOLD_MS = 120; // giữ rất ngắn để hạn chế cảm giác box "đuổi theo" vật thể

export function useDetection() {
  const [currentFrame, setCurrentFrame] = useState<string | Blob | null>(null);
  const [detections, setDetections]     = useState<Detection[]>([]);
  const [stats, setStats]               = useState<VehicleStats>(DEFAULT_STATS);
  const [streamActive, setStreamActive] = useState(false);
  const [companionActive, setCompanionActive] = useState(false);
  const [companionFrame, setCompanionFrame] = useState<string | Blob | null>(null);
  const [companionDetections, setCompanionDetections] = useState<Detection[]>([]);
  const [companionFps, setCompanionFps] = useState(0);
  const [companionLinePosition, setCompanionLinePosition] = useState<number>(DEFAULT_STATS.line_position);
  const [extraLive, setExtraLive] = useState<Record<number, { frame: string | Blob | null; dets: Detection[]; fps: number }>>({});
  const settingsTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const frameTimerRef = useRef<ReturnType<typeof setInterval>>();
  const companionTimerRef = useRef<ReturnType<typeof setInterval>>();
  const lastDetectionsRef = useRef<{ ts: number; dets: Detection[] }>({ ts: 0, dets: [] });

  // Load initial stats once
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

  // ── WebSocket handler ──────────────────────────────────────────────────────

  const handleWsMessage = useCallback((payload: FramePayload) => {
    // Extra streams (slot>=2) share the same WS channel with a different payload shape.
    const anyPayload = payload as any;
    const slot = typeof anyPayload.slot === 'number' ? anyPayload.slot : 0;
    if (slot >= 2) {
      if (!anyPayload.frame && !anyPayload.frame_blob) return;
      setExtraLive((prev) => ({
        ...prev,
        [slot]: {
          frame: anyPayload.frame_blob ?? anyPayload.frame ?? null,
          dets: anyPayload.detections ?? [],
          fps: anyPayload.fps ?? 0,
        },
      }));
      return;
    }

    if (payload.stats && !payload.stats.stream_active) {
      setStreamActive(false);
      return;
    }
    const incomingFrame = (payload as any).frame_blob ?? payload.frame ?? null;
    // Keep detection/frame in the same phase to reduce visual trailing.
    if (incomingFrame) {
      setCurrentFrame(incomingFrame);
      const next = payload.detections ?? [];
      const now = Date.now();
      if (next.length > 0) {
        lastDetectionsRef.current = { ts: now, dets: next };
        setDetections(next);
      } else {
        const age = now - (lastDetectionsRef.current.ts || 0);
        if (age <= DETECTION_HOLD_MS) {
          setDetections(lastDetectionsRef.current.dets);
        } else {
          setDetections([]);
        }
      }
    }
    setStats(payload.stats);
  }, []);

  const { connected: wsConnected, usingFallback } = useWebSocket({
    url: WS_URL,
    enabled: streamActive,
    onMessage: handleWsMessage,
    maxRetries: 5,
    retryDelay: 2000,
  });

  // ── Companion WebSocket (preferred) ───────────────────────────────────────
  const handleCompanionWs = useCallback((msg: any) => {
    if (!msg || (!msg.frame && !msg.frame_blob) || !msg.stream_active) {
      setCompanionFrame(null);
      setCompanionDetections([]);
      setCompanionFps(0);
      setCompanionLinePosition(DEFAULT_STATS.line_position);
      return;
    }
    setCompanionFrame(msg.frame_blob ?? msg.frame);
    setCompanionDetections(msg.detections ?? []);
    setCompanionFps(msg.fps ?? 0);
    const laneLine = Number(msg?.lane_stats?.line_position);
    if (Number.isFinite(laneLine) && laneLine >= 0 && laneLine <= 1) {
      setCompanionLinePosition(laneLine);
    }
  }, []);

  const { connected: companionWsConnected } = useWebSocket({
    url: WS_COMPANION_URL,
    enabled: streamActive && companionActive,
    onMessage: handleCompanionWs,
    maxRetries: 5,
    retryDelay: 2000,
  });

  // ── HTTP polling fallback (when WebSocket unavailable) ─────────────────────

  useEffect(() => {
    if (!streamActive || !usingFallback) {
      if (frameTimerRef.current) {
        clearInterval(frameTimerRef.current);
        frameTimerRef.current = undefined;
      }
      if (!streamActive) {
        setCurrentFrame(null);
        setDetections([]);
      }
      return;
    }

    let fetching = false;
    let emptyCount = 0;
    frameTimerRef.current = setInterval(async () => {
      if (fetching) return;
      fetching = true;
      try {
        const payload = await streamApi.getFrame();
        if (!payload) return;
        if (payload.stats && !payload.stats.stream_active) {
          setStreamActive(false);
          return;
        }
        if (payload.frame) {
          emptyCount = 0;
          setCurrentFrame(payload.frame);
          const next = payload.detections ?? [];
          const now = Date.now();
          if (next.length > 0) {
            lastDetectionsRef.current = { ts: now, dets: next };
            setDetections(next);
          } else {
            const age = now - (lastDetectionsRef.current.ts || 0);
            if (age <= DETECTION_HOLD_MS) {
              setDetections(lastDetectionsRef.current.dets);
            } else {
              setDetections([]);
            }
          }
        } else {
          emptyCount++;
          if (emptyCount > 20) {
            setStreamActive(false);
          }
        }
        setStats(payload.stats);
      } catch (err) {
        console.error('[useDetection] frame poll error:', err);
      } finally {
        fetching = false;
      }
    }, 250);

    return () => {
      if (frameTimerRef.current) {
        clearInterval(frameTimerRef.current);
        frameTimerRef.current = undefined;
      }
    };
  }, [streamActive, usingFallback]);

  // ── Companion stream polling fallback (when WS not connected) ─────────────

  useEffect(() => {
    if (!streamActive || !companionActive) {
      if (companionTimerRef.current) {
        clearInterval(companionTimerRef.current);
        companionTimerRef.current = undefined;
      }
      setCompanionFrame(null);
      setCompanionDetections([]);
      setCompanionFps(0);
      setCompanionLinePosition(DEFAULT_STATS.line_position);
      return;
    }
    // If WebSocket is connected, rely on pushed frames and avoid polling,
    // but keep the last rendered frame to prevent a blank panel during reconnects.
    if (companionWsConnected) {
      if (companionTimerRef.current) {
        clearInterval(companionTimerRef.current);
        companionTimerRef.current = undefined;
      }
      return;
    }

    let fetching2 = false;
    const tick = async () => {
      if (fetching2) return;
      fetching2 = true;
      try {
        const payload = await streamApi.getCompanionFrame();
        if (!payload || !payload.frame || !payload.stream_active) {
          setCompanionFrame(null);
          setCompanionDetections([]);
          setCompanionFps(0);
          setCompanionLinePosition(DEFAULT_STATS.line_position);
          return;
        }
        setCompanionFrame(payload.frame);
        setCompanionDetections(payload.detections ?? []);
        setCompanionFps(payload.fps ?? 0);
        const laneLine = Number((payload as any)?.lane_stats?.line_position);
        if (Number.isFinite(laneLine) && laneLine >= 0 && laneLine <= 1) {
          setCompanionLinePosition(laneLine);
        }
      } catch {
        // ignore
      } finally {
        fetching2 = false;
      }
    };

    window.setTimeout(() => void tick(), 125);
    companionTimerRef.current = setInterval(() => void tick(), 250);

    return () => {
      if (companionTimerRef.current) {
        clearInterval(companionTimerRef.current);
        companionTimerRef.current = undefined;
      }
    };
  }, [streamActive, companionActive, companionWsConnected]);

  // ── Actions ────────────────────────────────────────────────────────────────

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
  }, []);

  const stopStream = useCallback(async () => {
    await streamApi.stop();
    setStreamActive(false);
    setCompanionActive(false);
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

  const updateSettings = useCallback(
    (patch: Partial<Settings>) => {
      clearTimeout(settingsTimerRef.current);
      settingsTimerRef.current = setTimeout(async () => {
        await detectionApi.updateSettings(patch);
      }, 400);
    },
    []
  );

  return {
    // State
    currentFrame,
    detections,
    stats,
    companionFrame,
    companionDetections,
    companionFps,
    companionLinePosition,
    extraLive,
    wsConnected,
    usingFallback,
    streamActive,
    // Actions
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
