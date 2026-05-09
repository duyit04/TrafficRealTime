import type { Detection } from '../types/detection';
import { drawCountingLine, drawDetections } from '../utils/canvas';

type InitMsg = {
  type: 'init';
  canvas: OffscreenCanvas;
};

type FrameMsg = {
  type: 'frame';
  jpeg: ArrayBuffer; // transferable JPEG bytes
  detections: Detection[];
  line_position: number;
  showLine: boolean;
};

type ResizeMsg = {
  type: 'resize';
  width: number;
  height: number;
};

type Msg = InitMsg | FrameMsg | ResizeMsg;

let canvas: OffscreenCanvas | null = null;
let ctx: OffscreenCanvasRenderingContext2D | null = null;
let lastSize = { w: 0, h: 0 };

let decodeInFlight = false;
let pending: FrameMsg | null = null;
let renderedFrames = 0;
let lastReportAt = 0;

function ensureCanvasSize(w: number, h: number) {
  if (!canvas) return;
  const ww = Math.max(1, Math.floor(w));
  const hh = Math.max(1, Math.floor(h));
  if (ww === lastSize.w && hh === lastSize.h) return;
  canvas.width = ww;
  canvas.height = hh;
  lastSize = { w: ww, h: hh };
}

function renderBitmap(
  bmp: ImageBitmap,
  detections: Detection[],
  linePosition: number,
  showLine: boolean,
) {
  if (!canvas || !ctx) return;
  const cw = canvas.width;
  const ch = canvas.height;
  if (!(cw > 0 && ch > 0)) return;

  const imgRatio = bmp.width / bmp.height;
  const canRatio = cw / ch;
  let dw: number, dh: number, dx: number, dy: number;
  if (imgRatio > canRatio) {
    dw = cw;
    dh = cw / imgRatio;
    dx = 0;
    dy = (ch - dh) / 2;
  } else {
    dh = ch;
    dw = ch * imgRatio;
    dx = (cw - dw) / 2;
    dy = 0;
  }

  ctx.clearRect(0, 0, cw, ch);
  ctx.drawImage(bmp, dx, dy, dw, dh);

  const scaleX = dw / bmp.width;
  const scaleY = dh / bmp.height;
  ctx.save();
  ctx.translate(dx, dy);
  drawDetections(ctx as unknown as CanvasRenderingContext2D, detections, scaleX, scaleY);
  if (showLine && bmp.height > 0) {
    drawCountingLine(ctx as unknown as CanvasRenderingContext2D, bmp.height * linePosition * scaleY, dw);
  }
  ctx.restore();
  renderedFrames += 1;
  const now = Date.now();
  if (now - lastReportAt > 1000) {
    lastReportAt = now;
    (self as any).postMessage({ type: 'render_stats', renderedFrames });
  }
}

async function decodeAndRender(msg: FrameMsg) {
  if (!canvas || !ctx) return;

  if (decodeInFlight) {
    pending = msg;
    return;
  }
  decodeInFlight = true;
  try {
    const blob = new Blob([msg.jpeg], { type: 'image/jpeg' });
    const bmp = await createImageBitmap(blob);
    // Fallback: if size wasn't set yet, size to bitmap.
    if (canvas && (canvas.width <= 1 || canvas.height <= 1)) {
      ensureCanvasSize(bmp.width, bmp.height);
    }
    renderBitmap(bmp, msg.detections, msg.line_position, msg.showLine);
    bmp.close?.();
  } catch {
    // ignore
  } finally {
    decodeInFlight = false;
    if (pending) {
      const next = pending;
      pending = null;
      void decodeAndRender(next);
    }
  }
}

self.onmessage = (ev: MessageEvent<Msg>) => {
  const msg = ev.data;
  if (msg.type === 'init') {
    canvas = msg.canvas;
    ctx = canvas.getContext('2d', { alpha: false });
    return;
  }
  if (msg.type === 'resize') {
    ensureCanvasSize(msg.width, msg.height);
    return;
  }
  if (msg.type === 'frame') {
    void decodeAndRender(msg);
  }
};

