/**
 * useDetection – central hook using REST API only (no WebSocket).
 * Provides: stats and actions; frame/detections are not streamed in real-time anymore.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { detectionApi, streamApi } from '../services/api';
import type { Detection, VehicleStats, Settings } from '../types/detection';

const DEFAULT_STATS: VehicleStats = {
  total: 0,
  count_in: 0,
  count_out: 0,
  classes: {},
  classes_in: {},
  classes_out: {},
  counting_mode: 'all',
  fps: 0,
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

export function useDetection() {
  const [currentFrame, setCurrentFrame] = useState<string | null>(null);
  const [detections, setDetections]     = useState<Detection[]>([]);
  const [stats, setStats]               = useState<VehicleStats>(DEFAULT_STATS);
  const [streamActive, setStreamActive] = useState(false);
  const [companionActive, setCompanionActive] = useState(false);
  const [companionFrame, setCompanionFrame] = useState<string | null>(null);
  const [companionDetections, setCompanionDetections] = useState<Detection[]>([]);
  const [companionFps, setCompanionFps] = useState(0);
  const settingsTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const [wsConnected] = useState(false);
  const frameTimerRef = useRef<ReturnType<typeof setInterval>>();
  const companionTimerRef = useRef<ReturnType<typeof setInterval>>();

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

  // Poll latest frame while stream is active
  useEffect(() => {
    if (!streamActive) {
      if (frameTimerRef.current) {
        clearInterval(frameTimerRef.current);
        frameTimerRef.current = undefined;
      }
      if (companionTimerRef.current) {
        clearInterval(companionTimerRef.current);
        companionTimerRef.current = undefined;
      }
      setCurrentFrame(null);
      setDetections([]);
      setCompanionFrame(null);
      setCompanionDetections([]);
      setCompanionFps(0);
      return;
    }

    let fetching = false;
    let emptyCount = 0;
    frameTimerRef.current = setInterval(async () => {
      if (fetching) return;
      fetching = true;
      try {
        const payload = await streamApi.getFrame();

        // Backend returns frame:null when worker has stopped
        if (!payload || !payload.frame) {
          emptyCount++;
          // If 20 consecutive empty responses (~5s), stream is dead
          if (emptyCount > 20) {
            console.warn('[useDetection] stream appears dead, stopping poll');
            setStreamActive(false);
          }
          return;
        }
        emptyCount = 0;

        // Also check if backend says stream is no longer active
        if (payload.stats && !payload.stats.stream_active) {
          console.warn('[useDetection] backend reports stream inactive');
          setStreamActive(false);
          return;
        }

        setCurrentFrame(payload.frame);
        setDetections(payload.detections ?? []);
        setStats(payload.stats);
      } catch (err) {
        console.error('[useDetection] frame poll error:', err);
      } finally {
        fetching = false;
      }
    }, 250);

    // Companion poll (staggered a bit to avoid burst on backend)
    if (companionActive) {
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
            return;
          }
          setCompanionFrame(payload.frame);
          setCompanionDetections(payload.detections ?? []);
          setCompanionFps(payload.fps ?? 0);
        } catch {
          // ignore
        } finally {
          fetching2 = false;
        }
      };
      // Start slightly later than primary poll to spread load.
      window.setTimeout(() => void tick(), 125);
      companionTimerRef.current = setInterval(() => void tick(), 250);
    }

    return () => {
      if (frameTimerRef.current) {
        clearInterval(frameTimerRef.current);
        frameTimerRef.current = undefined;
      }
      if (companionTimerRef.current) {
        clearInterval(companionTimerRef.current);
        companionTimerRef.current = undefined;
      }
    };
  }, [streamActive, companionActive]);

  // ── Actions ────────────────────────────────────────────────────────────────

  const startStream = useCallback(async (url: string, opts?: { companionUrl?: string }) => {
    await streamApi.start({
      url,
      ...(opts?.companionUrl ? { companion_url: opts.companionUrl } : {}),
    });
    setCompanionActive(Boolean(opts?.companionUrl));
    setStreamActive(true);
  }, []);

  const stopStream = useCallback(async () => {
    await streamApi.stop();
    setStreamActive(false);
    setCompanionActive(false);
  }, []);

  const setRoi = useCallback(async (points: number[][]) => {
    await detectionApi.setRoi({ points, active: true });
  }, []);

  const clearRoi = useCallback(async () => {
    await detectionApi.clearRoi();
  }, []);

  const resetCount = useCallback(async () => {
    try {
      await detectionApi.resetStats();
      setStats((s) => ({ ...s, total: 0, classes: {}, congestion: DEFAULT_STATS.congestion }));
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
    wsConnected,
    streamActive,
    // Actions
    startStream,
    stopStream,
    reloadStats,
    setRoi,
    clearRoi,
    resetCount,
    updateSettings,
  };
}
