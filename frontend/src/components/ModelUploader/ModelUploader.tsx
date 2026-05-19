/**
 * TensorRT export panel + engine model loader.
 * - Chọn model .pt nguồn → export .engine
 * - Chọn .engine đã có → load / xóa
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import type { ModelInfo } from '../../types/detection';
import { modelApi } from '../../services/api';
import { IconBoltEngine, IconPlay, IconTrash } from '../icons/Icons';

function IconUpload({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="currentColor">
      <path fillRule="evenodd" d="M3 17a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM6.293 6.707a1 1 0 010-1.414l3-3a1 1 0 011.414 0l3 3a1 1 0 01-1.414 1.414L11 5.414V13a1 1 0 11-2 0V5.414L7.707 6.707a1 1 0 01-1.414 0z" clipRule="evenodd" />
    </svg>
  );
}

interface Props {
  models: ModelInfo[];
  onModelsChange: () => void;
  onToast: (msg: string, type: 'success' | 'error' | 'info') => void;
  onReloadStats: () => void;
  cudaAvailable?: boolean;
}

function shortName(name: string): string {
  return name.includes('/') ? name.split('/')[0] : name.replace(/\.(pt|engine)$/i, '');
}

export function ModelUploader({ models, onModelsChange, onToast, onReloadStats, cudaAvailable = true }: Props) {
  const ptModels    = models.filter((m) => /\.pt$/i.test(m.name));
  const engineModels = models.filter((m) => /\.engine$/i.test(m.name));

  const [selectedPt,     setSelectedPt]     = useState<string | null>(null);
  const [selectedEngine, setSelectedEngine] = useState<string | null>(null);

  const [exportingEngine,  setExportingEngine]  = useState(false);
  const [engineExportPct,  setEngineExportPct]  = useState(0);
  const [engineExportMsg,  setEngineExportMsg]  = useState('');
  const [loadAfterExport,  setLoadAfterExport]  = useState(true);
  const [exportFp16,       setExportFp16]       = useState(true);
  const [uploading,        setUploading]        = useState(false);
  const pollRef   = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileRef   = useRef<HTMLInputElement | null>(null);

  // Giữ selection hợp lệ khi danh sách thay đổi
  useEffect(() => {
    if (selectedPt     && !ptModels.find((m) => m.name === selectedPt))     setSelectedPt(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models]);
  useEffect(() => {
    if (selectedEngine && !engineModels.find((m) => m.name === selectedEngine)) setSelectedEngine(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models]);

  const activePt     = selectedPt     ?? ptModels[0]?.name     ?? '';
  const activeEngine = selectedEngine ?? engineModels[0]?.name ?? '';

  // ── Polling export status ──────────────────────────────────────────────────
  const stopPoll = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const pollOnce = useCallback(async (): Promise<boolean> => {
    try {
      const st  = await modelApi.getExportEngineStatus();
      const pct = Math.min(100, Math.max(0, Number(st.progress ?? 0)));
      const msg = typeof st.progress_message === 'string' ? st.progress_message.trim() : '';

      if (st.running) {
        setExportingEngine(true);
        setEngineExportPct(pct);
        setEngineExportMsg(msg || 'Đang export TensorRT…');
        return false;
      }
      if (!st.done) {
        setExportingEngine(false); setEngineExportPct(0); setEngineExportMsg('');
        return true;
      }
      if (st.ok && st.engine) {
        setEngineExportPct(100);
        setEngineExportMsg(msg || 'Export hoàn thành');
        onToast(`Hoàn thành: ${st.engine.split(/[/\\]/).pop() ?? st.engine}`, 'success');
        onModelsChange(); onReloadStats();
        window.setTimeout(() => { setExportingEngine(false); setEngineExportPct(0); setEngineExportMsg(''); }, 1400);
        return true;
      }
      onToast(st.error || 'Export thất bại', 'error');
      setExportingEngine(false); setEngineExportPct(0); setEngineExportMsg('');
      return true;
    } catch { return false; }
  }, [onToast, onModelsChange, onReloadStats]);

  const startPoll = useCallback(() => {
    stopPoll();
    pollRef.current = setInterval(async () => { if (await pollOnce()) stopPoll(); }, 1500);
  }, [pollOnce, stopPoll]);

  // Resume nếu export đang chạy khi mount
  useEffect(() => {
    let dead = false;
    (async () => {
      try {
        const st = await modelApi.getExportEngineStatus();
        if (dead || !st.running) return;
        setExportingEngine(true);
        setEngineExportPct(Math.min(100, Math.max(0, Number(st.progress ?? 0))));
        const m = typeof st.progress_message === 'string' ? st.progress_message.trim() : '';
        if (m) setEngineExportMsg(m);
        startPoll();
      } catch { /* ignore */ }
    })();
    return () => { dead = true; stopPoll(); };
  }, [startPoll, stopPoll]);

  // ── Export ─────────────────────────────────────────────────────────────────
  const handleExport = useCallback(async () => {
    if (!activePt) { onToast('Chưa có file .pt nào để export.', 'error'); return; }
    try {
      await modelApi.exportEngine({ name: activePt, load_after_export: loadAfterExport, fp16: exportFp16 });
      onToast('Đang export TensorRT… có thể mất vài phút.', 'info');
      setExportingEngine(true); setEngineExportPct(2); setEngineExportMsg('Đang chuẩn bị…');
      startPoll(); void pollOnce();
    } catch (e: unknown) {
      onToast(e instanceof Error ? e.message : 'Không bắt đầu được export', 'error');
      setExportingEngine(false);
    }
  }, [activePt, loadAfterExport, exportFp16, onToast, startPoll, pollOnce]);

  // ── Upload .pt ────────────────────────────────────────────────────────────
  const handleUpload = useCallback(async (file: File) => {
    if (!file.name.endsWith('.pt')) {
      onToast('Chỉ chấp nhận file .pt', 'error');
      return;
    }
    setUploading(true);
    try {
      await modelApi.upload(file);
      onToast(`Đã upload '${file.name}'`, 'success');
      onModelsChange();
      setSelectedPt(file.name);
    } catch (e: unknown) {
      onToast(e instanceof Error ? e.message : 'Upload thất bại', 'error');
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }, [onToast, onModelsChange]);

  return (
    <div className="flex flex-col gap-4">

      {/* ── Export TensorRT ───────────────────────────────────────── */}
      <div className="flex flex-col gap-2">

        {/* Chọn model .pt nguồn + upload */}
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-slate-500 shrink-0 w-12">Nguồn</span>
          {ptModels.length > 0 ? (
            <select
              className="flex-1 min-w-0 text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white text-slate-700"
              value={activePt}
              onChange={(e) => setSelectedPt(e.target.value)}
            >
              {ptModels.map((m) => (
                <option key={m.name} value={m.name} title={m.name}>
                  {shortName(m.name)}{m.active ? ' • active' : ''}
                </option>
              ))}
            </select>
          ) : (
            <span className="flex-1 text-[11px] text-slate-400 italic">Chưa có file .pt</span>
          )}
          {/* Hidden file input */}
          <input
            ref={fileRef}
            type="file"
            accept=".pt"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void handleUpload(f);
            }}
          />
          <button
            type="button"
            disabled={uploading || exportingEngine}
            title="Upload file .pt từ máy tính"
            onClick={() => fileRef.current?.click()}
            className="shrink-0 inline-flex items-center gap-1 px-2.5 py-1.5 text-[11px] font-semibold rounded-lg border border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <IconUpload className="h-3 w-3 shrink-0" aria-hidden />
            {uploading ? 'Uploading…' : 'Upload .pt'}
          </button>
        </div>

        {/* Options */}
        <div className="flex flex-wrap gap-3 pl-14">
          <label className="flex items-center gap-1.5 text-[11px] text-slate-600 cursor-pointer">
            <input type="checkbox" className="rounded border-slate-300 accent-accent"
              checked={loadAfterExport} onChange={(e) => setLoadAfterExport(e.target.checked)} />
            Load sau khi export
          </label>
          <label className="flex items-center gap-1.5 text-[11px] text-slate-600 cursor-pointer">
            <input type="checkbox" className="rounded border-slate-300 accent-accent"
              checked={exportFp16} onChange={(e) => setExportFp16(e.target.checked)} />
            FP16
          </label>
        </div>

        {/* Nút export */}
        <button
          type="button"
          disabled={!activePt || !cudaAvailable || exportingEngine}
          title={
            !cudaAvailable  ? 'Không phát hiện CUDA'
            : !activePt     ? 'Chưa có model .pt'
            : exportingEngine ? 'Đang export…'
            : 'Export sang TensorRT .engine'
          }
          onClick={() => void handleExport()}
          className="w-full inline-flex items-center justify-center gap-2 px-3 py-2 text-xs font-bold rounded-lg border border-amber-300 bg-amber-50 text-amber-900 hover:bg-amber-100 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <IconBoltEngine className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {exportingEngine ? 'Đang export .engine…' : 'Export TensorRT (.engine)'}
        </button>

        {/* Progress */}
        {exportingEngine && (
          <div className="rounded-lg border border-amber-200 bg-amber-50/80 px-2.5 py-2 space-y-1.5">
            <div className="flex items-start justify-between gap-2">
              <p className="text-[10px] text-amber-900 font-semibold leading-snug min-w-0">
                {engineExportMsg || 'Đang export…'}
              </p>
              <span className="shrink-0 text-[11px] font-black tabular-nums text-amber-900">
                {Math.round(engineExportPct)}%
              </span>
            </div>
            <div className="h-2 rounded-full bg-amber-200/80 overflow-hidden">
              <div
                className="h-full rounded-full bg-amber-500 transition-[width] duration-500 ease-out"
                style={{ width: `${Math.min(100, engineExportPct)}%` }}
                role="progressbar"
                aria-valuenow={Math.round(engineExportPct)}
                aria-valuemin={0} aria-valuemax={100}
              />
            </div>
            <p className="text-[9px] text-amber-800/80 leading-snug">
              % là ước lượng — lên 100% khi hoàn tất.
            </p>
          </div>
        )}
      </div>

      {/* ── Engine Models ─────────────────────────────────────────── */}
      <div className="pt-2 border-t border-slate-100 flex flex-col gap-1.5">
        <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold">
          Model .engine
        </p>
        {engineModels.length > 0 ? (
          <div className="flex items-center gap-2">
            <select
              className="flex-1 min-w-0 text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white text-slate-700"
              value={activeEngine}
              onChange={(e) => setSelectedEngine(e.target.value)}
            >
              {engineModels.map((m) => (
                <option key={m.name} value={m.name} title={m.name}>
                  {shortName(m.name)}{m.active ? ' • active' : ''}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={async () => {
                if (!activeEngine) return;
                try {
                  await modelApi.load(activeEngine);
                  onToast(`Loaded '${shortName(activeEngine)}'`, 'success');
                  onReloadStats(); onModelsChange();
                } catch (e: unknown) {
                  onToast(e instanceof Error ? e.message : 'Load thất bại', 'error');
                }
              }}
              className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-blue-100 text-accent hover:bg-blue-200"
            >
              <IconPlay className="h-3.5 w-3.5 shrink-0 opacity-90" aria-hidden />
              Load
            </button>
            <button
              type="button"
              onClick={async () => {
                if (!activeEngine) return;
                if (!confirm(`Xóa '${shortName(activeEngine)}'?`)) return;
                try {
                  await modelApi.delete(activeEngine);
                  onToast(`Đã xóa '${shortName(activeEngine)}'`, 'info');
                  onModelsChange(); setSelectedEngine(null);
                } catch (e: unknown) {
                  onToast(e instanceof Error ? e.message : 'Xóa thất bại', 'error');
                }
              }}
              className="inline-flex items-center justify-center px-2.5 py-1.5 text-xs font-semibold rounded-lg bg-red-100 text-danger hover:bg-red-200"
              aria-label="Xóa"
            >
              <IconTrash className="h-3.5 w-3.5 shrink-0" aria-hidden />
            </button>
          </div>
        ) : (
          <p className="text-[11px] text-slate-400 italic">Chưa có .engine — export ở trên.</p>
        )}
      </div>

    </div>
  );
}
