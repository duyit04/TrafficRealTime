/**
 * CameraWall – CCTV-style grid showing live thumbnails for all camera presets.
 * Each tile auto-refreshes every 8s. Click a tile to select that camera.
 */

import { useState, useEffect, useCallback, useRef } from 'react';

export interface CameraPreset {
  id: string;
  label: string;
  location: string;
  url: string;
}

interface TileState {
  frame: string | null;   // base64 JPEG
  loading: boolean;
  error: string | null;
  lastUpdated: number;
}

interface Props {
  cameras: readonly CameraPreset[];
  selectedUrl: string;
  activeUrl: string;        // currently streaming URL
  onSelect: (url: string) => void;
  onConnect: (url: string) => void;
  streamOn: boolean;
  connecting: boolean;
  /** Compact mode for embedded pickers (e.g. traffic-light RTSP modal) */
  variant?: 'default' | 'compact';
  /** Hide header row (title + auto-refresh) */
  showHeader?: boolean;
  /** Hide bottom hint text */
  showHint?: boolean;
  /** Grid columns */
  columns?: 1 | 2 | 3;
}

const REFRESH_INTERVAL = 8000; // ms between thumbnail refreshes

async function fetchThumbnail(url: string): Promise<{ ok: boolean; frame: string | null; error: string | null }> {
  const res = await fetch(`/api/v1/stream/thumbnail?url=${encodeURIComponent(url)}`);
  if (!res.ok) return { ok: false, frame: null, error: `HTTP ${res.status}` };
  return res.json();
}

export function CameraWall({
  cameras,
  selectedUrl,
  activeUrl,
  onSelect,
  onConnect,
  streamOn,
  connecting,
  variant = 'default',
  showHeader = true,
  showHint = true,
  columns = 2,
}: Props) {
  const [tiles, setTiles] = useState<Record<string, TileState>>(() =>
    Object.fromEntries(cameras.map((c) => [c.id, { frame: null, loading: true, error: null, lastUpdated: 0 }]))
  );
  const timersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const loadTile = useCallback(async (cam: CameraPreset) => {
    setTiles((prev) => ({ ...prev, [cam.id]: { ...prev[cam.id], loading: true, error: null } }));
    try {
      const data = await fetchThumbnail(cam.url);
      setTiles((prev) => ({
        ...prev,
        [cam.id]: { frame: data.frame, loading: false, error: data.ok ? null : (data.error ?? 'No frame'), lastUpdated: Date.now() },
      }));
    } catch (e: any) {
      setTiles((prev) => ({ ...prev, [cam.id]: { frame: null, loading: false, error: e.message, lastUpdated: Date.now() } }));
    }
    // Schedule next refresh
    timersRef.current[cam.id] = setTimeout(() => loadTile(cam), REFRESH_INTERVAL);
  }, []);

  useEffect(() => {
    cameras.forEach((cam) => loadTile(cam));
    return () => {
      Object.values(timersRef.current).forEach(clearTimeout);
    };
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex flex-col gap-2">
      {showHeader ? (
        <div className="flex items-center justify-between">
          <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold">
            Camera presets — {cameras.length} cameras
          </p>
          <span className="text-[10px] text-slate-400">auto-refresh 8s</span>
        </div>
      ) : null}

      <div className={`grid gap-2 ${columns === 1 ? 'grid-cols-1' : columns === 3 ? 'grid-cols-3' : 'grid-cols-2'}`}>
        {cameras.map((cam) => {
          const tile = tiles[cam.id];
          const isSelected = selectedUrl === cam.url;
          const isActive = activeUrl === cam.url && streamOn;

          return (
            <button
              key={cam.id}
              type="button"
              onClick={() => onSelect(cam.url)}
              onDoubleClick={() => onConnect(cam.url)}
              title={`${cam.label}\n${cam.url}\nDouble-click to connect`}
              className={`relative flex flex-col rounded-xl overflow-hidden border-2 transition-all text-left cursor-pointer focus:outline-none ${
                isActive
                  ? 'border-green-500 shadow-md shadow-green-500/20'
                  : isSelected
                  ? 'border-accent shadow-md shadow-blue-500/20'
                  : 'border-slate-200 hover:border-slate-400'
              }`}
            >
              {/* Thumbnail */}
              <div className="relative w-full bg-slate-900" style={{ aspectRatio: '16/9' }}>
                {tile?.frame ? (
                  <img
                    src={`data:image/jpeg;base64,${tile.frame}`}
                    alt={cam.label}
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center">
                    {tile?.loading ? (
                      <span className="text-slate-500 text-[10px] animate-pulse">Connecting...</span>
                    ) : (
                      <span className="text-slate-600 text-[10px]">Offline</span>
                    )}
                  </div>
                )}

                {/* Live badge */}
                {isActive && (
                  <span className="absolute top-1.5 left-1.5 flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-green-600 text-white text-[9px] font-bold">
                    <span className="w-1.5 h-1.5 rounded-full bg-white animate-pulse" />
                    LIVE
                  </span>
                )}

                {/* Loading spinner overlay */}
                {tile?.loading && tile?.frame && (
                  <div className="absolute inset-0 bg-black/20 flex items-center justify-center">
                    <span className="text-white text-[9px] animate-pulse">Refreshing...</span>
                  </div>
                )}

                {/* Error badge */}
                {tile?.error && !tile?.frame && (
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-slate-900">
                    <span className="text-red-400 text-lg">⚠</span>
                    <span className="text-slate-500 text-[9px] px-2 text-center truncate w-full">{tile.error}</span>
                  </div>
                )}

                {/* Selected overlay */}
                {isSelected && !isActive && (
                  <div className="absolute inset-0 bg-accent/10 border-0" />
                )}
              </div>

              {/* Label bar */}
              <div className={`${variant === 'compact' ? 'px-2 py-1' : 'px-2 py-1.5'} flex items-center gap-1.5 ${
                isActive ? 'bg-green-50' : isSelected ? 'bg-blue-50' : 'bg-white'
              }`}>
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                  isActive ? 'bg-green-500 animate-pulse' : isSelected ? 'bg-accent' : 'bg-slate-300'
                }`} />
                <span className={`text-[11px] font-semibold truncate ${
                  isActive ? 'text-green-700' : isSelected ? 'text-accent' : 'text-slate-700'
                }`}>
                  {cam.label}
                </span>
                {tile?.lastUpdated > 0 && (
                  <span className="ml-auto text-[9px] text-slate-400 shrink-0">
                    {new Date(tile.lastUpdated).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                  </span>
                )}
              </div>
            </button>
          );
        })}
      </div>

      {/* Connect hint */}
      {showHint ? (
        <p className="text-[10px] text-slate-400 text-center">
          Click để chọn · Double-click để kết nối ngay
        </p>
      ) : null}
    </div>
  );
}
