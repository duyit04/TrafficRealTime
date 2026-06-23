import { useState, useRef, useCallback, useEffect } from 'react';
import { mediaApi, type ImageDetectResult } from '../../services/api';
import { drawDetections } from '../../utils/canvas';
import type { Detection } from '../../types/detection';

function toDetections(
  items: ImageDetectResult['detections'],
): Detection[] {
  return items.map((d) => ({
    class_name: d.class_name,
    confidence: d.confidence,
    track_id: d.track_id,
    bbox: { x1: d.x1, y1: d.y1, x2: d.x2, y2: d.y2 },
  }));
}

/** Map bbox coords using the image's actual painted rect (handles object-contain letterboxing). */
function imagePaintLayout(img: HTMLImageElement) {
  const nw = img.naturalWidth;
  const nh = img.naturalHeight;
  const ew = img.clientWidth;
  const eh = img.clientHeight;
  if (nw <= 0 || nh <= 0 || ew <= 0 || eh <= 0) return null;

  const elRatio = ew / eh;
  const imgRatio = nw / nh;
  let dw: number;
  let dh: number;
  let dx: number;
  let dy: number;
  if (imgRatio > elRatio) {
    dw = ew;
    dh = ew / imgRatio;
    dx = 0;
    dy = (eh - dh) / 2;
  } else {
    dh = eh;
    dw = eh * imgRatio;
    dx = (ew - dw) / 2;
    dy = 0;
  }
  return { scaleX: dw / nw, scaleY: dh / nh, dx, dy, cw: ew, ch: eh };
}

export function ImageDetect() {
  const [result, setResult] = useState<ImageDetectResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const detectSeqRef = useRef(0);

  const openFilePicker = () => {
    if (!loading) inputRef.current?.click();
  };

  const redrawBoxes = useCallback(() => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img || !result?.detections.length) return;
    if (!img.complete || img.naturalWidth <= 0) return;

    const layout = imagePaintLayout(img);
    if (!layout) return;
    const { scaleX, scaleY, dx, dy, cw, ch } = layout;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
    canvas.style.width = `${cw}px`;
    canvas.style.height = `${ch}px`;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    ctx.save();
    ctx.translate(dx, dy);
    drawDetections(ctx, toDetections(result.detections), scaleX, scaleY);
    ctx.restore();
  }, [result]);

  useEffect(() => {
    redrawBoxes();
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => redrawBoxes());
    ro.observe(container);
    return () => ro.disconnect();
  }, [redrawBoxes, preview, result?.image]);

  useEffect(() => {
    return () => {
      if (preview) URL.revokeObjectURL(preview);
    };
  }, [preview]);

  const handleFile = async (file: File) => {
    const seq = ++detectSeqRef.current;
    setError(null);
    setResult(null);
    if (preview) URL.revokeObjectURL(preview);
    setPreview(URL.createObjectURL(file));
    setLoading(true);
    try {
      const res = await mediaApi.detectImage(file);
      if (seq !== detectSeqRef.current) return;
      setResult(res);
    } catch (e: any) {
      if (seq !== detectSeqRef.current) return;
      setError(e.message || 'Không thể detect ảnh');
    } finally {
      if (seq === detectSeqRef.current) setLoading(false);
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) void handleFile(file);
    e.target.value = '';
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const file = e.dataTransfer.files?.[0];
    if (file) handleFile(file);
  };

  const imageSrc =
    result?.image ? `data:image/jpeg;base64,${result.image}` : preview;

  return (
    <div className="flex flex-1 min-h-0 gap-4 p-4">
      <div className="flex flex-col flex-1 min-w-0 gap-3 min-h-0">
        <div
          className="flex flex-col items-center justify-center rounded-xl border-2 border-dashed border-slate-300 bg-slate-50 hover:border-accent hover:bg-blue-50/30 transition-colors cursor-pointer min-h-[100px] p-4 shrink-0"
          onClick={openFilePicker}
          onDrop={handleDrop}
          onDragOver={(e) => e.preventDefault()}
        >
          <input
            ref={inputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={handleChange}
          />
          <span className="text-2xl mb-1">🖼️</span>
          <span className="text-sm font-semibold text-slate-600">Kéo thả hoặc click để chọn ảnh</span>
          <span className="text-xs text-slate-400 mt-0.5">JPG, PNG, BMP, WebP…</span>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-slate-500 shrink-0">
            <svg className="h-4 w-4 animate-spin text-accent" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            Đang nhận diện…
          </div>
        ) : null}

        {error ? (
          <div className="px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-600 shrink-0">
            {error}
          </div>
        ) : null}

        {imageSrc ? (
          <div
            ref={containerRef}
            role="button"
            tabIndex={0}
            title="Bấm để chọn ảnh khác"
            className="relative flex-1 min-h-0 rounded-xl overflow-hidden border border-slate-200 bg-bg-base flex items-center justify-center cursor-pointer group"
            onClick={openFilePicker}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openFilePicker(); } }}
          >
            <div className="relative w-full h-full min-h-0">
              <img
                ref={imgRef}
                src={imageSrc}
                alt="Kết quả nhận diện"
                className="block w-full h-full object-contain"
                onLoad={redrawBoxes}
              />
              {result && result.detections.length > 0 ? (
                <canvas
                  ref={canvasRef}
                  className="absolute inset-0 w-full h-full pointer-events-none"
                />
              ) : null}
            </div>
            {result ? (
              <div className="absolute top-2 left-2 px-2 py-0.5 rounded-full bg-black/60 text-white text-xs font-bold z-10 pointer-events-none">
                {result.count} đối tượng
              </div>
            ) : null}
            {!loading ? (
              <div className="absolute bottom-2 right-2 px-2 py-1 rounded-lg bg-black/50 text-white/90 text-[10px] font-medium opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none">
                Bấm để chọn ảnh khác
              </div>
            ) : null}
            {loading ? (
              <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-2 bg-slate-900/60 pointer-events-none">
                <svg className="h-8 w-8 text-white animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                  <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                <span className="text-xs font-semibold text-white">Đang nhận diện…</span>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      {result && result.detections.length > 0 ? (
        <div className="w-52 shrink-0 flex flex-col gap-2 overflow-y-auto">
          <div className="text-xs font-bold uppercase tracking-wide text-slate-500 mb-1">
            Kết quả ({result.count})
          </div>
          {result.detections.map((d, i) => (
            <div
              key={i}
              className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs"
            >
              <div className="font-semibold text-slate-800 capitalize">{d.class_name}</div>
              <div className="text-slate-400 tabular-nums">
                conf: {(d.confidence * 100).toFixed(1)}%
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {result && result.detections.length === 0 && !loading ? (
        <div className="w-52 shrink-0 text-xs text-slate-400 italic">Không phát hiện đối tượng nào.</div>
      ) : null}
    </div>
  );
}
