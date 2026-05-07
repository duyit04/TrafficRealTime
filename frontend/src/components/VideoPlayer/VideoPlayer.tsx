/**
 * VideoPlayer – renders detection frames on canvas with overlays.
 * Receives base64 JPEG frames + detections from useDetection hook.
 */

import { useEffect, useRef, useCallback } from 'react';
import type { Detection, VehicleStats } from '../../types/detection';
import { drawDetections, drawCountingLine } from '../../utils/canvas';

interface Props {
  frame: string | null;          // base64 JPEG
  detections: Detection[];
  stats: VehicleStats;
  showLine?: boolean;
  onEmptyClick?: () => void;
}

export function VideoPlayer({ frame, detections, stats, showLine = true, onEmptyClick }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imgRef    = useRef<HTMLImageElement | null>(null);
  const prevSrcRef = useRef<string>('');

  const renderFrame = useCallback((img: HTMLImageElement) => {
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
    ctx.drawImage(img, dx, dy, dw, dh);

    const scaleX = dw / img.naturalWidth;
    const scaleY = dh / img.naturalHeight;

    ctx.save();
    ctx.translate(dx, dy);
    drawDetections(ctx, detections, scaleX, scaleY);
    if (showLine && img.naturalHeight > 0) {
      drawCountingLine(ctx, img.naturalHeight * stats.line_position * scaleY, dw);
    }
    ctx.restore();
  }, [detections, stats.line_position, showLine]);

  useEffect(() => {
    if (!frame) return;

    const src = `data:image/jpeg;base64,${frame}`;

    // If src is the same as last time, just redraw overlays (detections may have changed)
    if (src === prevSrcRef.current && imgRef.current?.complete) {
      renderFrame(imgRef.current);
      return;
    }

    prevSrcRef.current = src;

    // Create a NEW Image each time to guarantee onload fires
    const img = new Image();
    img.onload = () => {
      imgRef.current = img;
      renderFrame(img);
    };
    img.onerror = () => {
      console.error('[VideoPlayer] failed to decode frame');
    };
    img.src = src;
  }, [frame, detections, stats.line_position, showLine, renderFrame]);

  return (
    <div className="relative w-full h-full bg-white overflow-hidden">
      {!frame && (
        <button
          type="button"
          onClick={onEmptyClick}
          className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-slate-500 bg-white"
          style={{ cursor: onEmptyClick ? 'pointer' : 'default' }}
          aria-label="Chọn camera / kết nối stream"
        >
          <div className="w-20 h-20 rounded-full border-2 border-slate-300 flex items-center justify-center animate-pulse-slow">
            <span className="text-4xl">📹</span>
          </div>
          <p className="text-sm font-medium">Kết nối stream để xem camera</p>
        </button>
      )}
      {frame && (
        <div className="absolute top-3 right-3 flex items-center gap-1.5 bg-red-600 text-white text-[11px] font-bold px-2.5 py-1 rounded-full z-10 shadow">
          <span className="w-1.5 h-1.5 bg-white rounded-full animate-pulse" />
          LIVE
        </div>
      )}
      <canvas
        ref={canvasRef}
        className="w-full h-full block"
        style={{ display: frame ? 'block' : 'none' }}
      />
    </div>
  );
}
