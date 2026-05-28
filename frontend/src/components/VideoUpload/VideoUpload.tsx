import { useState, useRef, useEffect } from 'react';
import { mediaApi } from '../../services/api';
import { H264LivePlayer } from '../VideoPlayer';

interface Props {
  streamActive: boolean;
  onPlaybackStart: () => void;
  onPlaybackStop: () => void;
  onReloadStats?: () => void;
}

export function VideoUpload({ streamActive, onPlaybackStart, onPlaybackStop, onReloadStats }: Props) {
  const [running, setRunning] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [fileName, setFileName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [streamReady, setStreamReady] = useState(false);
  const [videoEnded, setVideoEnded] = useState(false);

  const handleFile = async (file: File) => {
    setError(null);
    setFileName(file.name);
    setUploading(true);
    try {
      await mediaApi.startVideo(file);
      setRunning(true);
      onPlaybackStart();
      onReloadStats?.();
    } catch (e: any) {
      setError(e.message || 'Không thể tải video lên');
      setFileName(null);
    } finally {
      setUploading(false);
    }
  };

  const handleStop = async () => {
    try {
      await mediaApi.stopVideo();
    } catch {
      // ignore
    }
    setRunning(false);
    setFileName(null);
    setVideoEnded(false);
    onPlaybackStop();
    onReloadStats?.();
  };

  // Detect natural end: backend sends stream_active=false while we're still running
  useEffect(() => {
    if (running && streamReady && !streamActive) {
      setRunning(false);
      setFileName(null);
      setVideoEnded(true);
      onPlaybackStop();
      onReloadStats?.();
    }
  }, [streamActive, running, streamReady]);

  useEffect(() => {
    if (streamActive) setStreamReady(true);
  }, [streamActive]);

  useEffect(() => {
    if (!running) setStreamReady(false);
  }, [running]);

  useEffect(() => {
    if (!running || streamReady) return;
    const id = window.setInterval(() => onReloadStats?.(), 500);
    return () => window.clearInterval(id);
  }, [running, streamReady, onReloadStats]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) handleFile(file);
    e.target.value = '';
  };

  const openFilePicker = () => {
    if (!uploading && !running) {
      setVideoEnded(false);
      inputRef.current?.click();
    }
  };

  return (
    <div className="flex flex-col flex-1 min-h-0 min-w-0 gap-2">
      <input
        ref={inputRef}
        type="file"
        accept="video/*,.ts,.m4v"
        className="hidden"
        onChange={handleChange}
      />

      {error ? (
        <div className="px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-600 shrink-0">
          {error}
        </div>
      ) : null}

      <div className="relative flex-1 min-h-0 overflow-hidden">
        <H264LivePlayer
          enabled={running}
          onSelectCamera={openFilePicker}
          placeholderTitle="Chọn video"
          placeholderSubtitle="Bấm để chọn file video"
        />

        {!running && uploading ? (
          <div className="absolute inset-0 z-30 flex flex-col items-center justify-center gap-3 bg-slate-100/90 pointer-events-none">
            <svg className="h-10 w-10 text-accent animate-spin" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            <span className="text-sm font-semibold text-slate-600">Đang tải lên…</span>
          </div>
        ) : null}

        {running ? (
          <div className="absolute top-3 left-3 z-30 flex items-center gap-2 max-w-[calc(100%-1.5rem)]">
            <button
              type="button"
              onClick={handleStop}
              className="shrink-0 px-3 py-1.5 rounded-lg border border-red-300 bg-red-50/95 text-red-600 text-xs font-semibold hover:bg-red-100 backdrop-blur-sm transition-colors"
            >
              ■ Dừng
            </button>
            {fileName ? (
              <span className="text-xs text-white/90 truncate drop-shadow" title={fileName}>
                {fileName}
              </span>
            ) : null}
            <span className="shrink-0 flex items-center gap-1 text-xs text-emerald-100 font-semibold drop-shadow">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              Đang phát
            </span>
          </div>
        ) : null}

        {running && !streamReady ? (
          <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-slate-900/75 backdrop-blur-sm pointer-events-none">
            <svg className="h-10 w-10 text-white animate-spin" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
              <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            <span className="text-sm font-semibold text-white">Đang xử lý video…</span>
          </div>
        ) : null}

        {videoEnded && !running ? (
          <div
            className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-slate-900/70 backdrop-blur-sm cursor-pointer"
            onClick={openFilePicker}
          >
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-emerald-500/20 ring-2 ring-emerald-400">
              <svg className="h-6 w-6 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </div>
            <span className="text-sm font-bold text-white">Video đã phát xong</span>
            <span className="text-xs text-white/60">Bấm để chọn video khác</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}
