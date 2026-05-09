import { useEffect, useRef } from 'react';
import { getWebSocketUrl } from '../../services/api';
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
  onError?: () => void;
  wsPath?: string;
}

export function H264LivePlayer({ enabled, onError, wsPath = '/ws/stream-h264' }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const playerRef = useRef<MpegTsPlayer | null>(null);
  const bootTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const firstFrameSeenRef = useRef(false);
  const lastFrameAtRef = useRef(0);
  const errorBurstRef = useRef<{ count: number; windowStart: number }>({ count: 0, windowStart: 0 });
  const onErrorRef = useRef<Props['onError']>(onError);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    if (!enabled) return;
    let dead = false;
    const wsUrl = getWebSocketUrl(wsPath);
    firstFrameSeenRef.current = false;
    lastFrameAtRef.current = Date.now();
    errorBurstRef.current = { count: 0, windowStart: 0 };
    let boundVideo: HTMLVideoElement | null = null;
    let markFrameSeen: (() => void) | null = null;

    const triggerFallback = () => {
      if (dead) return;
      dead = true;
      onErrorRef.current?.();
    };

    const boot = async () => {
      if (dead || !mpegts.isSupported()) {
        triggerFallback();
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

      const p = mpegts.createPlayer({
        type: 'mpegts',
        isLive: true,
        hasAudio: false,
        url: wsUrl,
      });
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
          if (b.count >= 3) triggerFallback();
        });
      }
      p.on('error', () => {
        const now = Date.now();
        const b = errorBurstRef.current;
        if (now - b.windowStart > 8000) {
          b.windowStart = now;
          b.count = 1;
        } else {
          b.count += 1;
        }
        // Allow short RTSP jitter/decode hiccups without dropping to JPEG immediately.
        if (b.count >= 4 && (now - lastFrameAtRef.current) > 4500) {
          triggerFallback();
        }
      });

      // If H264 relay is connected but no decodable frames arrive, avoid black screen.
      bootTimeoutRef.current = setTimeout(() => {
        if (!firstFrameSeenRef.current) triggerFallback();
      }, 5000);

      // Runtime watchdog: tolerate short stalls, fallback only on sustained freeze.
      watchdogRef.current = setInterval(() => {
        if (dead) return;
        const v = videoRef.current;
        if (!v) return;
        const idleMs = Date.now() - (lastFrameAtRef.current || 0);
        if (!firstFrameSeenRef.current && v.readyState < 2 && idleMs < 7000) return;
        if (idleMs > 7000 && v.readyState < 2) {
          triggerFallback();
        }
      }, 2500);
    };

    void boot();
    return () => {
      dead = true;
      if (bootTimeoutRef.current) {
        clearTimeout(bootTimeoutRef.current);
        bootTimeoutRef.current = null;
      }
      if (watchdogRef.current) {
        clearInterval(watchdogRef.current);
        watchdogRef.current = null;
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
      <video ref={videoRef} className="w-full h-full object-contain" muted autoPlay playsInline />
      <div className="absolute top-3 right-3 flex items-center gap-1.5 bg-indigo-600 text-white text-[11px] font-bold px-2.5 py-1 rounded-full z-10 shadow">
        H264
      </div>
    </div>
  );
}

