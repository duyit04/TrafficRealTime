/**
 * VideoPlayer – renders detection frames on canvas with overlays.
 * Receives base64 JPEG frames + detections from useDetection hook.
 */

import { useEffect, useMemo, useRef, useCallback } from 'react';
import type { Detection, VehicleStats } from '../../types/detection';
import { drawDetections, drawCountingLine } from '../../utils/canvas';
import { IconCameraCctv } from '../icons/Icons';

type WorkerEntry = { worker: Worker; releaseTimer?: ReturnType<typeof setTimeout> };
const workerByCanvas = new WeakMap<HTMLCanvasElement, WorkerEntry>();

interface Props {
  frame: string | Blob | null;   // base64 JPEG (polling) or Blob (binary WS)
  detections: Detection[];
  stats: VehicleStats;
  showLine?: boolean;
  onEmptyClick?: () => void;
  overlayOnly?: boolean; // draw detections only, no base frame
  showLiveBadge?: boolean;
}

export function VideoPlayer({
  frame,
  detections,
  stats,
  showLine = true,
  onEmptyClick,
  overlayOnly = false,
  showLiveBadge = true,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imgRef    = useRef<HTMLImageElement | null>(null);
  const prevSrcRef = useRef<string>('');
  const prevBlobRef = useRef<Blob | null>(null);
  const decodeInFlightRef = useRef(false);
  const pendingBlobRef = useRef<Blob | null>(null);
  const decodeTokenRef = useRef(0);
  const workerRef = useRef<Worker | null>(null);
  const workerReadyRef = useRef(false);
  const workerAliveRef = useRef(false);
  const workerLastFrameAtRef = useRef(0);
  const offscreenTransferredRef = useRef(false);
  const trackMotionRef = useRef<Record<number, { cx: number; cy: number; t: number }>>({});
  // Motion compensation can over-shoot on low-FPS / jittery streams and visually de-sync boxes.
  // Keep it disabled by default to prioritize geometric correctness.
  const ENABLE_H264_MOTION_COMPENSATION = false;
  const MOTION_BASE_LEAD_MS = 45;
  const MOTION_MAX_LEAD_MS = 240;
  const MOTION_MAX_LEAD_PX = 40;
  const MOTION_MIN_DT_MS = 25;
  const MOTION_MAX_DT_MS = 500;

  const applyMotionCompensation = useCallback((input: Detection[]): Detection[] => {
    // Only compensate when drawing overlay-only on top of H264.
    // JPEG path already carries frame+detections in the same packet.
    if (!overlayOnly || !ENABLE_H264_MOTION_COMPENSATION) return input;
    if (!input.length) return input;
    const inferMs = Number(stats.avg_inference_ms || 0);
    const fps = Number(stats.fps || 0);
    const frameMs = fps > 0 ? (1000 / fps) : 80;
    const leadMs = Math.max(
      MOTION_BASE_LEAD_MS,
      Math.min(MOTION_MAX_LEAD_MS, inferMs * 0.85 + frameMs * 0.6)
    );
    const now = Date.now();
    const nextState: Record<number, { cx: number; cy: number; t: number }> = {};
    const out = input.map((det) => {
      const tid = det.track_id;
      const cx = (det.bbox.x1 + det.bbox.x2) * 0.5;
      const cy = (det.bbox.y1 + det.bbox.y2) * 0.5;
      if (tid == null) return det;
      const prev = trackMotionRef.current[tid];
      nextState[tid] = { cx, cy, t: now };
      if (!prev) return det;
      const dt = now - prev.t;
      if (dt < MOTION_MIN_DT_MS || dt > MOTION_MAX_DT_MS) return det;
      const vx = (cx - prev.cx) / dt;
      const vy = (cy - prev.cy) / dt;
      let dx = vx * leadMs;
      let dy = vy * leadMs;
      dx = Math.max(-MOTION_MAX_LEAD_PX, Math.min(MOTION_MAX_LEAD_PX, dx));
      dy = Math.max(-MOTION_MAX_LEAD_PX, Math.min(MOTION_MAX_LEAD_PX, dy));
      return {
        ...det,
        bbox: {
          x1: det.bbox.x1 + dx,
          y1: det.bbox.y1 + dy,
          x2: det.bbox.x2 + dx,
          y2: det.bbox.y2 + dy,
        },
      };
    });
    trackMotionRef.current = nextState;
    return out;
  }, [overlayOnly, stats.avg_inference_ms, stats.fps]);

  function base64ToArrayBuffer(b64: string): ArrayBuffer {
    const bin = atob(b64);
    const len = bin.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }

  const canUseWorker = useMemo(() => {
    return (
      !overlayOnly &&
      typeof Worker !== 'undefined' &&
      typeof OffscreenCanvas !== 'undefined' &&
      typeof HTMLCanvasElement !== 'undefined' &&
      typeof (HTMLCanvasElement.prototype as any).transferControlToOffscreen === 'function'
    );
  }, [overlayOnly]);

  // Init worker renderer (one worker per VideoPlayer instance).
  useEffect(() => {
    if (!canUseWorker) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    // React StrictMode may mount/unmount effects twice in dev.
    // transferControlToOffscreen() can only be called once per canvas, so we reuse a shared worker per canvas.
    const existing = workerByCanvas.get(canvas);
    if (existing) {
      if (existing.releaseTimer) clearTimeout(existing.releaseTimer);
      workerRef.current = existing.worker;
      workerReadyRef.current = true;
      workerAliveRef.current = false;
    } else {
      const off = (canvas as any).transferControlToOffscreen?.();
      if (!off) return;
      offscreenTransferredRef.current = true;
      const w = new Worker(new URL('../../workers/videoRender.worker.ts', import.meta.url), { type: 'module' });
      workerByCanvas.set(canvas, { worker: w });
      workerRef.current = w;
      workerReadyRef.current = false;
      workerAliveRef.current = false;
      w.postMessage({ type: 'init', canvas: off }, [off]);
      workerReadyRef.current = true;
    }

    const w2 = workerRef.current;
    if (w2) {
      w2.onmessage = (ev: MessageEvent) => {
        const d: any = ev.data;
        if (d && d.type === 'render_stats') {
          workerAliveRef.current = true;
          workerLastFrameAtRef.current = Date.now();
        }
      };
    }

    const ro = new ResizeObserver(() => {
      const c = canvasRef.current;
      if (!c || !workerRef.current) return;
      const width = c.offsetWidth || c.parentElement?.clientWidth || 1;
      const height = c.offsetHeight || c.parentElement?.clientHeight || 1;
      workerRef.current.postMessage({ type: 'resize', width, height });
    });
    ro.observe(canvas);
    // Send initial size immediately (ResizeObserver may not fire right away)
    {
      const width = canvas.offsetWidth || canvas.parentElement?.clientWidth || 1;
      const height = canvas.offsetHeight || canvas.parentElement?.clientHeight || 1;
      workerRef.current.postMessage({ type: 'resize', width, height });
    }
    // Some layouts report 0x0 on first tick; retry a few times.
    const t1 = window.setTimeout(() => {
      const c = canvasRef.current;
      if (!c || !workerRef.current) return;
      const width = c.offsetWidth || c.parentElement?.clientWidth || 1;
      const height = c.offsetHeight || c.parentElement?.clientHeight || 1;
      workerRef.current.postMessage({ type: 'resize', width, height });
    }, 120);
    const t2 = window.setTimeout(() => {
      const c = canvasRef.current;
      if (!c || !workerRef.current) return;
      const width = c.offsetWidth || c.parentElement?.clientWidth || 1;
      const height = c.offsetHeight || c.parentElement?.clientHeight || 1;
      workerRef.current.postMessage({ type: 'resize', width, height });
    }, 600);

    return () => {
      ro.disconnect();
      clearTimeout(t1);
      clearTimeout(t2);
      // Delay termination a bit so StrictMode re-mount can reuse the same OffscreenCanvas control + worker.
      const entry = workerByCanvas.get(canvas);
      if (entry) {
        entry.releaseTimer = setTimeout(() => {
          try { entry.worker.terminate(); } catch {}
          workerByCanvas.delete(canvas);
        }, 750);
      }
      workerReadyRef.current = false;
      workerAliveRef.current = false;
      workerRef.current = null;
    };
  }, [canUseWorker]);

  const renderFrame = useCallback((img: HTMLImageElement, detsForDraw: Detection[]) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Fit canvas to container
    const cw = canvas.offsetWidth  || canvas.parentElement?.clientWidth  || img.naturalWidth;
    const ch = canvas.offsetHeight || canvas.parentElement?.clientHeight || img.naturalHeight;
    if (cw <= 0 || ch <= 0) return;

    canvas.width  = cw;
    canvas.height = ch;

    // Letterbox
    const imgRatio = img.naturalWidth / img.naturalHeight;
    const canRatio = cw / ch;
    let dw: number, dh: number, dx: number, dy: number;

    if (imgRatio > canRatio) {
      dw = cw; dh = cw / imgRatio; dx = 0; dy = (ch - dh) / 2;
    } else {
      dh = ch; dw = ch * imgRatio; dx = (cw - dw) / 2; dy = 0;
    }

    ctx.clearRect(0, 0, cw, ch);
    if (!overlayOnly) {
      ctx.drawImage(img, dx, dy, dw, dh);
    }

    const scaleX = dw / img.naturalWidth;
    const scaleY = dh / img.naturalHeight;

    ctx.save();
    ctx.translate(dx, dy);
    drawDetections(ctx, detsForDraw, scaleX, scaleY);
    if (showLine && img.naturalHeight > 0) {
      drawCountingLine(ctx, img.naturalHeight * stats.line_position * scaleY, dw);
    }
    ctx.restore();
  }, [stats.line_position, showLine, overlayOnly]);

  useEffect(() => {
    const compensatedDetections = applyMotionCompensation(detections);
    if (!frame) return;

    // Binary WS path: decode off main thread when possible
    if (frame instanceof Blob) {
      // Offload decode+render to worker (required once canvas is transferred).
      if (canUseWorker && workerRef.current && workerReadyRef.current) {
        void frame.arrayBuffer().then((ab) => {
          workerRef.current?.postMessage(
            {
              type: 'frame',
              jpeg: ab,
              detections: compensatedDetections,
              line_position: stats.line_position,
              showLine,
            },
            [ab]
          );
        });
        return;
      }

      // If canvas is transferred but worker isn't available, we can't render on main thread.
      if (offscreenTransferredRef.current) return;

      // Fall back to main-thread render for this instance (non-OffscreenCanvas browsers only).
      if (prevBlobRef.current === frame && imgRef.current?.complete) {
        renderFrame(imgRef.current, compensatedDetections);
        return;
      }
      prevBlobRef.current = frame;

      // If decode is busy, keep only the latest blob (drop older frames).
      if (decodeInFlightRef.current) {
        pendingBlobRef.current = frame;
        return;
      }

      const myToken = ++decodeTokenRef.current;
      decodeInFlightRef.current = true;

      void (async () => {
        try {
          const bmp = await createImageBitmap(frame);
          if (myToken !== decodeTokenRef.current) return;
          // Draw bitmap directly to canvas, then overlays.
          const canvas = canvasRef.current;
          if (!canvas) return;
          const ctx = canvas.getContext('2d');
          if (!ctx) return;

          const cw = canvas.offsetWidth || canvas.parentElement?.clientWidth || bmp.width;
          const ch = canvas.offsetHeight || canvas.parentElement?.clientHeight || bmp.height;
          if (cw <= 0 || ch <= 0) return;
          canvas.width = cw;
          canvas.height = ch;

          const imgRatio = bmp.width / bmp.height;
          const canRatio = cw / ch;
          let dw: number, dh: number, dx: number, dy: number;
          if (imgRatio > canRatio) {
            dw = cw; dh = cw / imgRatio; dx = 0; dy = (ch - dh) / 2;
          } else {
            dh = ch; dw = ch * imgRatio; dx = (cw - dw) / 2; dy = 0;
          }

          ctx.clearRect(0, 0, cw, ch);
          if (!overlayOnly) {
            ctx.drawImage(bmp, dx, dy, dw, dh);
          }

          const scaleX = dw / bmp.width;
          const scaleY = dh / bmp.height;
          ctx.save();
          ctx.translate(dx, dy);
          drawDetections(ctx, compensatedDetections, scaleX, scaleY);
          if (showLine && bmp.height > 0) {
            drawCountingLine(ctx, bmp.height * stats.line_position * scaleY, dw);
          }
          ctx.restore();
        } catch {
          // ignore decode errors
        } finally {
          decodeInFlightRef.current = false;
          const pending = pendingBlobRef.current;
          pendingBlobRef.current = null;
          // If a newer frame arrived while decoding, trigger a repaint via state update path.
          // We can't set state here; instead, rely on the next useEffect run. If pending exists,
          // we can advance the token so older draws are ignored.
          if (pending) {
            // Bump token so any late draws are ignored, and let effect rerun naturally.
            // (React will rerun effect on next frame update anyway.)
            decodeTokenRef.current += 1;
          }
        }
      })();

      return;
    }

    // Base64 path (polling/fallback). If canvas is transferred, send to worker too.
    if (typeof frame === 'string' && canUseWorker && workerRef.current && workerReadyRef.current) {
      try {
        const ab = base64ToArrayBuffer(frame);
        workerRef.current.postMessage(
          {
            type: 'frame',
            jpeg: ab,
            detections: compensatedDetections,
            line_position: stats.line_position,
            showLine,
          },
          [ab]
        );
        return;
      } catch {
        // ignore and try legacy path
      }
    }

    const src = `data:image/jpeg;base64,${frame}`;

    // If src is the same as last time, just redraw overlays (detections may have changed)
    if (src === prevSrcRef.current && imgRef.current?.complete) {
      renderFrame(imgRef.current, compensatedDetections);
      return;
    }

    prevSrcRef.current = src;

    // Create a NEW Image each time to guarantee onload fires
    const img = new Image();
    img.onload = () => {
      imgRef.current = img;
      renderFrame(img, compensatedDetections);
    };
    img.onerror = () => {
      console.error('[VideoPlayer] failed to decode frame');
    };
    img.src = src;
  }, [frame, detections, stats.line_position, showLine, renderFrame, canUseWorker, overlayOnly, applyMotionCompensation]);

  return (
    <div className={`relative w-full h-full overflow-hidden ${overlayOnly ? 'bg-transparent' : 'bg-white'}`}>
      {!overlayOnly && !frame && (
        <button
          type="button"
          onClick={onEmptyClick}
          className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-slate-500 bg-white"
          style={{ cursor: onEmptyClick ? 'pointer' : 'default' }}
          aria-label="Chọn camera / kết nối stream"
        >
          <IconCameraCctv className="h-10 w-10 text-slate-400" aria-hidden />
          <p className="text-sm font-medium text-slate-600">Kết nối stream để xem camera</p>
        </button>
      )}
      {!overlayOnly && showLiveBadge && frame && (
        <div className="absolute top-3 right-3 flex items-center gap-1.5 bg-red-600 text-white text-[11px] font-bold px-2.5 py-1 rounded-full z-10 shadow">
          <span className="w-1.5 h-1.5 bg-white rounded-full animate-pulse" />
          LIVE
        </div>
      )}
      <canvas
        ref={canvasRef}
        className="w-full h-full block"
        style={{ display: (overlayOnly || frame) ? 'block' : 'none' }}
      />
    </div>
  );
}
