/**
 * ModelUploader – drag-and-drop upload + models library + TensorRT export.
 */

import { useState, useCallback, useEffect, useRef, type DragEvent } from 'react';
import type { ModelInfo } from '../../types/detection';
import { modelApi } from '../../services/api';
import { IconBoltEngine, IconModelLayers, IconPlay, IconTrash } from '../icons/Icons';

interface Props {
  models: ModelInfo[];
  onModelsChange: () => void;
  onToast: (msg: string, type: 'success' | 'error' | 'info') => void;
  onReloadStats: () => void;
  /** Export .engine cần CUDA + TensorRT trên server */
  cudaAvailable?: boolean;
}

export function ModelUploader({ models, onModelsChange, onToast, onReloadStats, cudaAvailable = true }: Props) {
  const [progress, setProgress] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [exportingEngine, setExportingEngine] = useState(false);
  const [engineExportPct, setEngineExportPct] = useState(0);
  const [engineExportMsg, setEngineExportMsg] = useState('');
  const [loadEngineAfterExport, setLoadEngineAfterExport] = useState(true);
  const [exportFp16, setExportFp16] = useState(true);
  const exportPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopExportPoll = useCallback(() => {
    if (exportPollRef.current) {
      clearInterval(exportPollRef.current);
      exportPollRef.current = null;
    }
  }, []);

  const pollExportStatusOnce = useCallback(async (): Promise<boolean> => {
    try {
      const st = await modelApi.getExportEngineStatus();
      const pctRaw = Number(st.progress ?? 0);
      const pct = Number.isFinite(pctRaw) ? Math.min(100, Math.max(0, pctRaw)) : 0;
      const msgFromServer =
        typeof st.progress_message === 'string' ? st.progress_message.trim() : '';

      if (st.running) {
        setExportingEngine(true);
        setEngineExportPct(pct);
        setEngineExportMsg(msgFromServer || 'Đang export TensorRT (.engine)…');
        return false;
      }

      /* Không chạy: idle hoặc đã xong */
      if (!st.done) {
        setExportingEngine(false);
        setEngineExportPct(0);
        setEngineExportMsg('');
        return true;
      }

      if (st.ok && st.engine) {
        setEngineExportPct(100);
        setEngineExportMsg(msgFromServer || 'Export hoàn thành');
        const base = st.engine.split(/[/\\]/).pop() ?? st.engine;
        onToast(`Hoàn thành export TensorRT: ${base}`, 'success');
        onModelsChange();
        onReloadStats();
        window.setTimeout(() => {
          setExportingEngine(false);
          setEngineExportPct(0);
          setEngineExportMsg('');
        }, 1400);
        return true;
      }

      setEngineExportPct(pct);
      onToast(st.error || 'Export TensorRT thất bại', 'error');
      setExportingEngine(false);
      setEngineExportPct(0);
      setEngineExportMsg('');
      return true;
    } catch {
      return false;
    }
  }, [onToast, onModelsChange, onReloadStats]);

  const startExportPolling = useCallback(() => {
    stopExportPoll();
    exportPollRef.current = setInterval(async () => {
      const shouldStop = await pollExportStatusOnce();
      if (shouldStop) stopExportPoll();
    }, 1500);
  }, [pollExportStatusOnce, stopExportPoll]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const st = await modelApi.getExportEngineStatus();
        if (cancelled || !st.running) return;
        setExportingEngine(true);
        setEngineExportPct(Math.min(100, Math.max(0, Number(st.progress ?? 0))));
        const m =
          typeof st.progress_message === 'string' ? st.progress_message.trim() : '';
        if (m) setEngineExportMsg(m);
        startExportPolling();
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
      stopExportPoll();
    };
  }, [startExportPolling, stopExportPoll]);

  const selectedName = selectedModel ?? models[0]?.name ?? '';
  const isPtSelected = /\.pt$/i.test(selectedName);

  const upload = useCallback(
    (file: File) => {
      if (!file.name.endsWith('.pt')) {
        onToast('Chỉ chấp nhận file .pt!', 'error');
        return;
      }

      setUploading(true);
      setProgress(0);

      // Use XHR for upload progress
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      form.append('file', file);

      xhr.open('POST', '/api/v1/models/upload');
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) setProgress(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        setUploading(false);
        try {
          const data = JSON.parse(xhr.responseText);
          if (data.success) {
            onToast(data.message, 'success');
            onModelsChange();
            onReloadStats();
          } else {
            onToast(data.detail || 'Upload thất bại', 'error');
          }
        } catch {
          onToast('Server error', 'error');
        }
      };
      xhr.onerror = () => { setUploading(false); onToast('Network error', 'error'); };
      xhr.send(form);
    },
    [onToast, onModelsChange, onReloadStats]
  );

  const handleDrop = useCallback(
    (e: DragEvent<HTMLLabelElement>) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files[0];
      if (file) upload(file);
    },
    [upload]
  );

  const handleExportEngine = useCallback(async () => {
    const name = selectedModel ?? models[0]?.name;
    if (!name || !/\.pt$/i.test(name)) {
      onToast('Chỉ export được từ file .pt (chọn model trong danh sách).', 'error');
      return;
    }
    try {
      await modelApi.exportEngine({
        name,
        load_after_export: loadEngineAfterExport,
        fp16: exportFp16,
      });
      onToast('Đang export TensorRT (.engine) trên server… Có thể mất vài phút.', 'info');
      setExportingEngine(true);
      setEngineExportPct(2);
      setEngineExportMsg('Đang chuẩn bị…');
      startExportPolling();
      void pollExportStatusOnce();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Không bắt đầu được export';
      onToast(msg, 'error');
      setExportingEngine(false);
    }
  }, [
    models,
    selectedModel,
    loadEngineAfterExport,
    exportFp16,
    onToast,
    startExportPolling,
    pollExportStatusOnce,
  ]);

  return (
    <div className="flex flex-col gap-3">
      {/* Upload zone */}
      <label
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className={`flex flex-col items-center gap-2 p-4 rounded-xl border-2 border-dashed cursor-pointer transition-all text-center ${
          dragOver
            ? 'border-accent bg-blue-50'
            : 'border-slate-200 bg-slate-50 hover:border-accent/50 hover:bg-blue-50/50'
        }`}
      >
        <input
          type="file"
          accept=".pt"
          className="hidden"
          onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
        />
        <IconModelLayers className="h-10 w-10 text-accent/80 shrink-0" aria-hidden />
        <span className="text-xs text-slate-600">
          Drag &amp; drop <strong className="text-slate-800">*.pt</strong> (YOLOv3/v8/v26) hoặc click
        </span>
      </label>

      {/* Upload progress */}
      {uploading && (
        <div>
          <div className="h-1.5 rounded-full bg-slate-200 overflow-hidden">
            <div
              className="h-full bg-accent transition-all rounded-full"
              style={{ width: `${progress}%` }}
            />
          </div>
          <p className="text-xs text-slate-500 mt-1">Uploading… {progress}%</p>
        </div>
      )}

      {/* Models library – always visible */}
      <div className="flex flex-col gap-1.5">
        <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold">Models</p>
        {models.length > 0 ? (
          <>
          <div className="flex items-center gap-2">
            <select
              className="flex-1 min-w-0 text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white text-slate-700"
              value={selectedModel ?? (models[0]?.name ?? '')}
              onChange={(e) => setSelectedModel(e.target.value)}
            >
              {models.map((m) => {
                const displayName = m.name.includes('/')
                  ? m.name.split('/')[0]
                  : m.name.replace(/\.(pt|engine)$/i, '');
                return (
                  <option key={m.name} value={m.name} title={m.name}>
                    {displayName}{m.active ? ' • active' : ''}
                  </option>
                );
              })}
            </select>
            <button
              type="button"
              title="Load selected model"
              onClick={async () => {
                const name = selectedModel ?? models[0]?.name;
                if (!name) return;
                try {
                  await modelApi.load(name);
                  onToast(`Loaded '${name}'`, 'success');
                  onReloadStats();
                  onModelsChange();
                } catch (e: any) {
                  onToast(e.message, 'error');
                }
              }}
              className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-blue-100 text-accent hover:bg-blue-200"
            >
              <IconPlay className="h-3.5 w-3.5 shrink-0 opacity-90" aria-hidden />
              Load
            </button>
            <button
              type="button"
              title="Delete selected model"
              onClick={async () => {
                const name = selectedModel ?? models[0]?.name;
                if (!name) return;
                if (!confirm(`Xóa '${name}'?`)) return;
                try {
                  await modelApi.delete(name);
                  onToast(`Deleted '${name}'`, 'info');
                  onModelsChange();
                  setSelectedModel(null);
                } catch (e: any) {
                  onToast(e.message, 'error');
                }
              }}
              className="inline-flex items-center justify-center px-2.5 py-1.5 text-xs font-semibold rounded-lg bg-red-100 text-danger hover:bg-red-200"
              aria-label="Xóa model đã chọn"
            >
              <IconTrash className="h-3.5 w-3.5 shrink-0" aria-hidden />
            </button>
          </div>
          <div className="mt-2 pt-2 border-t border-slate-100 space-y-2">
            <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold">TensorRT (.engine)</p>
            <p className="text-[11px] text-slate-500 leading-snug">
              Chuyển model <strong className="text-slate-700">.pt</strong> sang <strong className="text-slate-700">.engine</strong> (YOLO → TensorRT).
              Cần GPU + CUDA trên máy chạy backend.
            </p>
            <label className="flex items-center gap-2 text-[11px] text-slate-600 cursor-pointer">
              <input
                type="checkbox"
                className="rounded border-slate-300 accent-accent"
                checked={loadEngineAfterExport}
                onChange={(e) => setLoadEngineAfterExport(e.target.checked)}
              />
              Load file .engine vừa tạo làm model đang dùng
            </label>
            <label className="flex items-center gap-2 text-[11px] text-slate-600 cursor-pointer">
              <input
                type="checkbox"
                className="rounded border-slate-300 accent-accent"
                checked={exportFp16}
                onChange={(e) => setExportFp16(e.target.checked)}
              />
              FP16 (khuyến nghị, nhanh hơn trên GPU)
            </label>
            <button
              type="button"
              disabled={
                !isPtSelected ||
                !cudaAvailable ||
                exportingEngine ||
                models.length === 0
              }
              title={
                !cudaAvailable
                  ? 'Không phát hiện CUDA — export TensorRT cần GPU'
                  : !isPtSelected
                    ? 'Chọn một file .pt trong danh sách Models'
                    : exportingEngine
                      ? 'Đang export…'
                      : 'Export sang TensorRT .engine'
              }
              onClick={() => void handleExportEngine()}
              className="w-full inline-flex items-center justify-center gap-2 px-3 py-2 text-xs font-bold rounded-lg border border-amber-300 bg-amber-50 text-amber-900 hover:bg-amber-100 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <IconBoltEngine className="h-3.5 w-3.5 shrink-0" aria-hidden />
              {exportingEngine ? 'Đang export .engine…' : 'Export TensorRT (.engine)'}
            </button>
            {exportingEngine ? (
              <div className="rounded-lg border border-amber-200 bg-amber-50/80 px-2.5 py-2 space-y-1.5">
                <div className="flex items-start justify-between gap-2">
                  <p className="text-[10px] text-amber-900 font-semibold leading-snug min-w-0">
                    {engineExportMsg || 'Đang export TensorRT…'}
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
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label="Tiến độ export TensorRT"
                  />
                </div>
                <p className="text-[9px] text-amber-800/90 leading-snug">
                  Phần trăm là ước lượng trong lúc build; kết thúc sẽ lên 100% và có thông báo.
                </p>
              </div>
            ) : null}
          </div>
          </>
        ) : (
          <button
            type="button"
            onClick={async () => {
              try {
                await modelApi.load('default');
                onToast('Loading default model...', 'info');
                onReloadStats();
                onModelsChange();
              } catch (e: any) {
                onToast(e.message, 'error');
              }
            }}
            className="w-full inline-flex items-center justify-center gap-1.5 py-2 text-xs font-semibold rounded-lg bg-blue-100 text-accent hover:bg-blue-200 transition-all"
          >
            <IconPlay className="h-3.5 w-3.5 shrink-0 opacity-90" aria-hidden />
            Load Default Model
          </button>
        )}
      </div>
    </div>
  );
}
