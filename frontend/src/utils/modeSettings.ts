import type { Settings } from '../types/detection';

export type AppMode = 'rtsp' | 'image' | 'video';

export interface ModeProfile {
  settings: Settings;
  countingEnabled: boolean;
}

export const DEFAULT_SETTINGS: Settings = {
  conf_threshold: 0.35,
  line_position: 0.55,
  max_fps: 30,
  skip_frames: 0,
  tracker_type: 'bytetrack',
  congestion_threshold: 10,
  congestion_duration: 5,
};

const STORAGE_KEY = 'traffic_monitor_mode_profiles';

export function defaultModeProfiles(): Record<AppMode, ModeProfile> {
  return {
    rtsp: {
      settings: { ...DEFAULT_SETTINGS },
      countingEnabled: true,
    },
    image: {
      settings: {
        ...DEFAULT_SETTINGS,
        skip_frames: 0,
      },
      countingEnabled: false,
    },
    video: {
      settings: {
        ...DEFAULT_SETTINGS,
        skip_frames: 0,
        // max_fps: 30,
      },
      countingEnabled: true,
    },
  };
}

export function loadModeProfiles(): Record<AppMode, ModeProfile> {
  const base = defaultModeProfiles();
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return base;
    const parsed = JSON.parse(raw) as Partial<Record<AppMode, Partial<ModeProfile>>>;
    (['rtsp', 'image', 'video'] as AppMode[]).forEach((mode) => {
      const hit = parsed[mode];
      if (hit?.settings) {
        base[mode].settings = { ...DEFAULT_SETTINGS, ...hit.settings };
      }
      if (typeof hit?.countingEnabled === 'boolean') {
        base[mode].countingEnabled = hit.countingEnabled;
      }
    });
  } catch {
    // ignore corrupt storage
  }
  return base;
}

export function saveModeProfiles(profiles: Record<AppMode, ModeProfile>): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(profiles));
  } catch {
    // ignore quota errors
  }
}
