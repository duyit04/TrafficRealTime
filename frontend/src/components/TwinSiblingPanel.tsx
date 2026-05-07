/**
 * Preview RTSP (ảnh MJPEG làm mới định kỳ — không realtime như luồng LIVE + AI).
 * Nhiều màn: lệch thời điểm gọi API + làm mới thưa hơn để tránh nghẽn máy chủ / NVR.
 */

import { useEffect, useState } from 'react';

/** Làm mới cơ bản một màn phụ (~3,6s khi chỉ có một preview). */
const DEFAULT_REFRESH_MS = 3600;
const STAGGER_BETWEEN_PANELS_MS = 750;

async function fetchThumbnail(url: string, width: number, fast: boolean): Promise<string | null> {
  try {
    const qs = new URLSearchParams({
      url,
      width: String(width),
      fast: fast ? 'true' : 'false',
    });
    const res = await fetch(`/api/v1/stream/thumbnail?${qs}`);
    if (!res.ok) return null;
    const data = await res.json();
    return data.ok && data.frame ? (data.frame as string) : null;
  } catch {
    return null;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => window.setTimeout(r, ms));
}

export function getPreviewStaggerMs(panelIndex: number): number {
  return panelIndex * STAGGER_BETWEEN_PANELS_MS;
}

/** Chu kỳ làm mới (lâu hơn khi nhiều màn để giảm tải burst RTSP). */
export function getPreviewRefreshMs(extraPanelCount: number): number {
  if (extraPanelCount <= 0) return DEFAULT_REFRESH_MS;
  return Math.min(12000, 4200 + extraPanelCount * 900);
}

interface Props {
  url: string;
  label: string;
  /** Ẩn khung tiêu đề phía trên (khi Dashboard đã có hàng « Màn 2 »). */
  showHeader?: boolean;
  className?: string;
  /** px rộng tối đa JPEG backend (nhỏ hơn ↔ nhẹ ↔ nhanh hơn). */
  thumbMaxWidth?: number;
  refreshMs?: number;
  /** Trì hoãn lần gọi đầu để không mở cùng lúc nhiều RTSP với các màn khác. */
  staggerMs?: number;
}

export function TwinSiblingPanel({
  url,
  label,
  showHeader = true,
  className = '',
  thumbMaxWidth = 720,
  refreshMs = DEFAULT_REFRESH_MS,
  staggerMs = 0,
}: Props) {
  const [frame, setFrame] = useState<string | null>(null);

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    let tid: ReturnType<typeof setTimeout>;

    const loop = async () => {
      const img = await fetchThumbnail(url, thumbMaxWidth, true);
      if (!cancelled) setFrame(img);
      if (!cancelled) tid = window.setTimeout(loop, refreshMs);
    };

    const start = async () => {
      if (staggerMs > 0) await sleep(staggerMs);
      if (cancelled) return;
      void loop();
    };

    void start();
    return () => {
      cancelled = true;
      clearTimeout(tid);
    };
  }, [url, refreshMs, staggerMs, thumbMaxWidth]);

  return (
    <div className={`flex flex-col gap-2 h-full min-h-0 min-w-0 ${className}`}>
      {showHeader ? (
        <div className="bg-slate-50/90 px-2 py-1.5">
          <p className="text-[10px] font-bold uppercase tracking-wider text-slate-600 truncate" title={label}>
            Màn 2 — preview RTSP
          </p>
        </div>
      ) : null}
      <div className="relative flex-1 min-h-[200px] min-w-0 overflow-hidden bg-white flex items-center justify-center">
        {frame ? (
          <img
            src={`data:image/jpeg;base64,${frame}`}
            alt={label}
            className="w-full h-full object-contain"
            decoding="async"
          />
        ) : (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-[10px] text-slate-500 p-4 text-center bg-white">
            <span>Đang tải preview…</span>
            <span className="text-[9px] text-slate-400">Ảnh làm mới định kỳ (~vài giây), không cùng tốc độ với màn LIVE</span>
          </div>
        )}
        <div className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-2 px-2 py-1 bg-white/90 backdrop-blur-sm">
          <span className="text-[10px] font-semibold text-slate-800 truncate">{label}</span>
          <span className="shrink-0 rounded-full bg-slate-200 px-2 py-0.5 text-[9px] font-bold text-slate-700">
            Preview
          </span>
        </div>
      </div>
    </div>
  );
}
