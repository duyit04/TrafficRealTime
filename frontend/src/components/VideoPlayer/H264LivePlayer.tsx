import { useEffect, useRef } from 'react';
import { getWebSocketUrl } from '../../services/api';
import { IconCameraCctv } from '../icons/Icons';
import mpegts from 'mpegts.js';

type MpegTsPlayer = {
  attachMediaElement: (el: HTMLVideoElement) => void;
  load: () => void;
  play: () => Promise<void> | void;
  pause: () => void;
  unload: () => void;
  detachMediaElement: () => void;
  destroy: () => void;
  on: (event: string, cb: (...args: unknown[]) => void) => void;
};

interface Props {
  enabled: boolean;
  wsPath?: string;
  /** Khi chưa bật stream — bấm vùng placeholder */
  onSelectCamera?: () => void;
  placeholderTitle?: string;
  placeholderSubtitle?: string;
}

export function H264LivePlayer({
  enabled,
  wsPath = '/ws/stream-h264',
  onSelectCamera,
  placeholderTitle = 'Chọn camera để xem',
  placeholderSubtitle = 'Bấm để mở Camera / Stream',
}: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const playerRef = useRef<MpegTsPlayer | null>(null);
  const bootTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firstFrameSeenRef = useRef(false);
  const lastFrameAtRef = useRef(0);
  const errorBurstRef = useRef<{ count: number; windowStart: number }>({ count: 0, windowStart: 0 });
  useEffect(() => {
    if (!enabled) return;
    let dead = false;
    const wsUrl = getWebSocketUrl(wsPath);
    firstFrameSeenRef.current = false;
    lastFrameAtRef.current = Date.now();
    errorBurstRef.current = { count: 0, windowStart: 0 };
    let boundVideo: HTMLVideoElement | null = null;
    let markFrameSeen: (() => void) | null = null;
    let lagInterval: ReturnType<typeof setInterval> | null = null;

    const boot = async () => {
      if (dead || !mpegts.isSupported()) {
        return;
      }
      const video = videoRef.current;
      if (!video) return;

      markFrameSeen = () => {
        firstFrameSeenRef.current = true;
        lastFrameAtRef.current = Date.now();
      };
      boundVideo = video;
      video.addEventListener('loadeddata', markFrameSeen);
      video.addEventListener('playing', markFrameSeen);
      video.addEventListener('timeupdate', markFrameSeen);

      const p = mpegts.createPlayer(
        {
          type: 'mpegts',
          isLive: true,
          hasAudio: false,
          url: wsUrl,
        },
        {
          enableWorker: true,
          enableStashBuffer: false,
          liveBufferLatencyChasing: true,
          liveBufferLatencyMaxLatency: 2.0,
          liveBufferLatencyMinRemain: 0.2,
          autoCleanupSourceBuffer: true,
          autoCleanupMaxBackwardDuration: 4,
          autoCleanupMinBackwardDuration: 2,
        },
      );
      playerRef.current = p;
      p.attachMediaElement(video);
      p.load();
      const playResult = p.play();
      if (playResult && typeof (playResult as Promise<void>).catch === 'function') {
        (playResult as Promise<void>).catch((err: unknown) => {
          if (dead) return;
          const msg = err instanceof Error ? err.message : String(err ?? '');
          // Ignore expected interruption during fast remount/cleanup.
          if (msg.includes('interrupted by a call to pause')) return;
          // Single transient play failure can recover on next segment; only fallback on bursts.
          const now = Date.now();
          const b = errorBurstRef.current;
          if (now - b.windowStart > 8000) {
            b.windowStart = now;
            b.count = 1;
          } else {
            b.count += 1;
          }
        });
      }
      p.on('error', () => {
        // H264 only — log/debounce errors, no JPEG fallback.
      });

      // Safety-net: seek to live edge if lag exceeds 3s (catches edge cases mpegts latency chasing misses)
      lagInterval = setInterval(() => {
        if (dead || !video || !video.buffered.length) return;
        const liveEdge = video.buffered.end(video.buffered.length - 1);
        const lag = liveEdge - video.currentTime;
        if (lag > 3.0) {
          video.currentTime = liveEdge - 0.3;
        }
      }, 4000);

      bootTimeoutRef.current = setTimeout(() => {
        if (!firstFrameSeenRef.current) {
          console.warn('[H264LivePlayer] no frames yet:', wsPath);
        }
      }, 12000);
    };

    void boot();
    return () => {
      dead = true;
      if (lagInterval) {
        clearInterval(lagInterval);
        lagInterval = null;
      }
      if (bootTimeoutRef.current) {
        clearTimeout(bootTimeoutRef.current);
        bootTimeoutRef.current = null;
      }
      if (boundVideo && markFrameSeen) {
        try { boundVideo.removeEventListener('loadeddata', markFrameSeen); } catch {}
        try { boundVideo.removeEventListener('playing', markFrameSeen); } catch {}
        try { boundVideo.removeEventListener('timeupdate', markFrameSeen); } catch {}
      }
      const p = playerRef.current;
      if (p) {
        try { p.pause(); } catch {}
        try { p.unload(); } catch {}
        try { p.detachMediaElement(); } catch {}
        try { p.destroy(); } catch {}
      }
      playerRef.current = null;
    };
  }, [enabled, wsPath]);

  return (
    <div className="relative w-full h-full bg-black overflow-hidden">
      {!enabled && (
        <button
          type="button"
          onClick={onSelectCamera}
          className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-2.5 text-slate-500 bg-slate-100 hover:bg-slate-50 transition-colors"
          style={{ cursor: onSelectCamera ? 'pointer' : 'default' }}
          aria-label={placeholderTitle}
        >
          <IconCameraCctv className="h-10 w-10 text-slate-400" aria-hidden />
          <p className="text-sm font-medium text-slate-600">{placeholderTitle}</p>
          <p className="text-[11px] text-slate-400">{placeholderSubtitle}</p>
        </button>
      )}
      <video
        ref={videoRef}
        className="w-full h-full object-contain"
        muted
        autoPlay
        playsInline
        style={{ visibility: enabled ? 'visible' : 'hidden' }}
      />
      {enabled ? (
        <div className="absolute top-3 right-3 flex items-center gap-1.5 bg-indigo-600 text-white text-[11px] font-bold px-2.5 py-1 rounded-full z-10 shadow">
          H264
        </div>
      ) : null}
    </div>
  );
}

