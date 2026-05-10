/**
 * RoiDrawer – transparent canvas overlay for drawing ROI polygon.
 * Allows user to click points, undo, and submit to backend.
 * Supports controlled mode (pass points, setPoints, isDrawing, setIsDrawing) for sharing state with RoiCanvasOverlay.
 */

import { useRef, useState, useEffect, useCallback, type MouseEvent } from 'react';
import { drawRoi } from '../../utils/canvas';
import { IconPencil, IconCheck, IconUndo, IconTrash } from '../icons/Icons';

export type RoiPoint = { x: number; y: number };

interface Props {
  onApply: (points: number[][]) => void;
  onClear: () => void;
  active: boolean;
  /** Controlled mode: when provided, use these instead of internal state (for sharing with overlay). */
  points?: RoiPoint[];
  setPoints?: (points: RoiPoint[] | ((prev: RoiPoint[]) => RoiPoint[])) => void;
  isDrawing?: boolean;
  setIsDrawing?: (value: boolean) => void;
}

export function RoiDrawer({ onApply, onClear, active, points: controlledPoints, setPoints: controlledSetPoints, isDrawing: controlledDrawing, setIsDrawing: controlledSetDrawing }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [internalPoints, setInternalPoints] = useState<RoiPoint[]>([]);
  const [internalDrawing, setInternalDrawing] = useState(false);

  const isControlled = controlledPoints != null && controlledSetPoints != null && controlledDrawing != null && controlledSetDrawing != null;
  const points = isControlled ? controlledPoints! : internalPoints;
  const setPoints = isControlled ? controlledSetPoints! : setInternalPoints;
  const isDrawing = isControlled ? controlledDrawing! : internalDrawing;
  const setIsDrawing = isControlled ? controlledSetDrawing! : setInternalDrawing;

  // Redraw whenever points or active state change
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d')!;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawRoi(ctx, points, active);
  }, [points, active]);

  // Resize canvas to match parent
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ro = new ResizeObserver(() => {
      canvas.width  = canvas.offsetWidth;
      canvas.height = canvas.offsetHeight;
      const ctx = canvas.getContext('2d')!;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      drawRoi(ctx, points, active);
    });
    ro.observe(canvas.parentElement!);
    return () => ro.disconnect();
  }, [points, active]);

  const handleClick = useCallback(
    (e: MouseEvent<HTMLCanvasElement>) => {
      if (!isDrawing) return;
      const rect = canvasRef.current!.getBoundingClientRect();
      setPoints((prev) => [
        ...prev,
        { x: e.clientX - rect.left, y: e.clientY - rect.top },
      ]);
    },
    [isDrawing]
  );

  const handleDoubleClick = useCallback(() => {
    if (isDrawing) setIsDrawing(false);
  }, [isDrawing]);

  const undo = useCallback(() => setPoints((p) => p.slice(0, -1)), []);

  const clear = useCallback(() => {
    setPoints([]);
    setIsDrawing(false);
    onClear();
  }, [onClear]);

  const apply = useCallback(() => {
    if (points.length < 3) return;
    // Convert canvas coords to video pixel coords via naturalWidth hack:
    // For simplicity, we send canvas-relative coords (backend scales by ROI active flag)
    const coords = points.map((p) => [Math.round(p.x), Math.round(p.y)]);
    onApply(coords);
    setIsDrawing(false);
  }, [points, onApply]);

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-slate-500">
        Click trên video để thêm điểm ROI • Double-click để hoàn thành
      </p>

      <div className="flex gap-2 flex-wrap">
        <button
          onClick={() => setIsDrawing(!isDrawing)}
          className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border transition-all ${
            isDrawing
              ? 'bg-accent text-white border-accent'
              : 'bg-white text-slate-600 border-slate-200 hover:border-accent hover:text-accent'
          }`}
        >
          {isDrawing ? (
            <>
              <IconCheck className="h-3.5 w-3.5 shrink-0" aria-hidden /> Done
            </>
          ) : (
            <>
              <IconPencil className="h-3.5 w-3.5 shrink-0" aria-hidden /> Draw
            </>
          )}
        </button>
        <button
          onClick={undo}
          disabled={points.length === 0}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-slate-200 text-slate-600 hover:border-accent hover:text-accent transition-all disabled:opacity-30"
        >
          <IconUndo className="h-3.5 w-3.5 shrink-0" aria-hidden /> Undo
        </button>
        <button
          onClick={clear}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-slate-200 text-slate-600 hover:border-danger hover:text-danger transition-all"
        >
          <IconTrash className="h-3.5 w-3.5 shrink-0" aria-hidden /> Clear
        </button>
        <button
          onClick={apply}
          disabled={points.length < 3}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg bg-accent text-white border border-accent transition-all hover:bg-blue-700 disabled:opacity-30"
        >
          <IconCheck className="h-3.5 w-3.5 shrink-0" aria-hidden /> Apply ROI
        </button>
      </div>

      <div className="flex items-center gap-2 text-xs text-slate-500">
        <span>{points.length} điểm</span>
        <span
          className={`px-2 py-0.5 rounded-full text-[10px] font-bold border ${
            active
              ? 'text-accent border-accent/30 bg-blue-50'
              : 'text-slate-400 border-slate-200 bg-slate-50'
          }`}
        >
          {active ? 'Active' : 'Inactive'}
        </span>
      </div>

    </div>
  );
}

// ── Canvas-only overlay for positioning over the video (no duplicate controls) ──

export interface RoiCanvasOverlayProps {
  points: RoiPoint[];
  setPoints: (points: RoiPoint[] | ((prev: RoiPoint[]) => RoiPoint[])) => void;
  isDrawing: boolean;
  setIsDrawing: (value: boolean) => void;
  onApply: (points: number[][]) => void;
  onClear: () => void;
  active: boolean;
  /** Optional external ref so parent can read canvas sizing for coordinate mapping. */
  canvasRefExternal?: React.RefObject<HTMLCanvasElement>;
}

export function RoiCanvasOverlay({ points, setPoints, isDrawing, setIsDrawing, onApply, onClear, active, canvasRefExternal }: RoiCanvasOverlayProps) {
  const internalRef = useRef<HTMLCanvasElement>(null);
  const canvasRef = canvasRefExternal ?? internalRef;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d')!;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawRoi(ctx, points, active);
  }, [points, active]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ro = new ResizeObserver(() => {
      canvas.width = canvas.offsetWidth;
      canvas.height = canvas.offsetHeight;
      const ctx = canvas.getContext('2d')!;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      drawRoi(ctx, points, active);
    });
    ro.observe(canvas.parentElement!);
    return () => ro.disconnect();
  }, [points, active]);

  const handleClick = useCallback(
    (e: MouseEvent<HTMLCanvasElement>) => {
      if (!isDrawing) return;
      const rect = canvasRef.current!.getBoundingClientRect();
      setPoints([
        ...points,
        { x: e.clientX - rect.left, y: e.clientY - rect.top },
      ]);
    },
    [isDrawing, points, setPoints]
  );

  const handleDoubleClick = useCallback(() => {
    if (isDrawing) setIsDrawing(false);
  }, [isDrawing, setIsDrawing]);

  return (
    <canvas
      ref={canvasRef}
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      className={`absolute inset-0 w-full h-full ${
        isDrawing ? 'cursor-crosshair' : 'pointer-events-none'
      }`}
      style={{ zIndex: 10 }}
      aria-hidden
    />
  );
}
