/**
 * Dashboard – main page assembling all components.
 * Uses useDetection hook for all state management.
 */

import { useState, useEffect, useCallback, useMemo, useRef, type ReactNode } from 'react';
import { IconAlertJam, IconCameraCctv, IconRoiFrame, IconTune } from '../../components/icons/Icons';
import { useDetection } from '../../hooks/useDetection';
import { H264LivePlayer } from '../../components/VideoPlayer';
import { RoiDrawer, RoiCanvasOverlay } from '../../components/RoiDrawer';
import type { RoiPoint } from '../../components/RoiDrawer';
import { ModelUploader } from '../../components/ModelUploader';
import { CounterPanel } from '../../components/CounterPanel';
import { CameraWall } from '../../components/CameraWall/CameraWall';
import { TwinSiblingPanel, getPreviewRefreshMs, getPreviewStaggerMs } from '../../components/TwinSiblingPanel';
import { TrafficLightPanel } from '../../components/TrafficLight';
import { modelApi, detectionApi, streamApi, trafficLightApi } from '../../services/api';
import type { DeviceInfo } from '../../services/api';
import type { ModelInfo, Settings, Toast } from '../../types/detection';

// ── Camera presets (control room) ─────────────────────────────────────────────
const CAMERA_PRESETS = [
  { id: 'cam-1201', label: 'Camera 12 – Cổng chính',   location: 'KCN DD', url: 'rtsp://hctech:Admin@789@kcndd.cameraddns.net:554/Streaming/channels/1201' },
  { id: 'cam-501',  label: 'Camera 5 – Lane 501 (cùng tuyến 601)', location: 'KCN DD', url: 'rtsp://hctech:Admin@789@kcndd.cameraddns.net:554/Streaming/channels/501' },
  { id: 'cam-601',  label: 'Camera 6 – Lane 601 (cùng tuyến 501)', location: 'KCN DD', url: 'rtsp://hctech:Admin@789@kcndd.cameraddns.net:554/Streaming/channels/601' },
  { id: 'cam-401',  label: 'Camera 4 – Nội khu A',     location: 'KCN DD', url: 'rtsp://hctech:Admin@789@kcndd.cameraddns.net:554/Streaming/channels/401' },
  { id: 'cam-101',  label: 'Camera 1 – Ngã tư trung tâm', location: 'KCN DD', url: 'rtsp://hctech:Admin@789@kcndd.cameraddns.net:554/Streaming/channels/101' },
  { id: 'cam-2701', label: 'Camera 27 – Đường vòng',   location: 'KCN DD', url: 'rtsp://hctech:Admin@789@kcndd.cameraddns.net:554/Streaming/channels/2701' },
] as const;

/** Hai nhãn cạnh cột đèn: màn 1 = preset luồng chính, màn 2 = camera cặp hoặc mô tả chia ROI. */
function phaseRoadTitlesFromPrimaryUrl(primaryUrl: string): [string, string] {
  const u = primaryUrl.trim();
  const preset = CAMERA_PRESETS.find((c) => c.url === u);
  const line1 =
    preset?.label ?? (u ? 'Camera 1 — luồng đang kết nối' : 'Camera 1 — chưa chọn camera');
  const line2 = 'Camera 2 — chưa chọn camera';
  return [line1, line2];
}

const MAX_EXTRA_PREVIEW_COLS = 4;

// ── Toast helper ──────────────────────────────────────────────────────────────
let toastId = 0;

export function Dashboard() {
  const {
    detections, stats, wsConnected, companionWsConnected, companionActive, companionStreamActive,
    companionDetections, companionLinePosition,
    extraLive,
    startStream, startCompanion, stopCompanion, stopStream, reloadStats, setRoi, clearRoi, setRoiSlot, clearRoiSlot, resetCount, updateSettings,
  } = useDetection();

  const [streamUrl, setStreamUrl]     = useState('');
  const [connecting, setConnecting]   = useState(false);
  const [streamOn, setStreamOn]       = useState(false);
  const [models, setModels]           = useState<ModelInfo[]>([]);
  const [settings, setSettings]       = useState<Settings>({
    conf_threshold: 0.35,
    line_position: 0.55,
    max_fps: 30,
    skip_frames: 0,
    tracker_type: 'bytetrack',
    counting_mode: 'all',
    congestion_threshold: 10,
    congestion_duration: 5,
  });
  const [toasts, setToasts]           = useState<Toast[]>([]);
  type RoiSlotKey = 'primary' | 'companion' | 2 | 3;
  type RoiSlotState = { active: boolean; points: RoiPoint[]; drawing: boolean };
  const [roiTarget, setRoiTarget] = useState<RoiSlotKey>('primary');
  const [roiBySlot, setRoiBySlot] = useState<Record<string, RoiSlotState>>({
    primary: { active: false, points: [], drawing: false },
    companion: { active: false, points: [], drawing: false },
    2: { active: false, points: [], drawing: false },
    3: { active: false, points: [], drawing: false },
  });
  const roiCanvasPrimaryRef = useRef<HTMLCanvasElement>(null);
  const roiCanvasCompanionRef = useRef<HTMLCanvasElement>(null);
  const roiCanvasExtra2Ref = useRef<HTMLCanvasElement>(null);
  const roiCanvasExtra3Ref = useRef<HTMLCanvasElement>(null);
  const [countingEnabled, setCountingEnabled] = useState(true);
  const [deviceInfo, setDeviceInfo] = useState<DeviceInfo>({ cuda_available: false, device_name: null });
  /** Số màn preview RTSP thêm cạnh luồng chính (0 = chỉ một màn LIVE). */
  const [extraPreviewCount, setExtraPreviewCount] = useState(0);
  /** URL camera gán thủ công cho từng màn phụ (index 0 = màn 2). Rỗng = auto. */
  const [extraPreviewUrls, setExtraPreviewUrls] = useState<string[]>([]);
  /** Khi khác null: đang gán camera cho màn phụ (0 = màn 2). */
  const [assignExtraIndex, setAssignExtraIndex] = useState<number | null>(null);
  /** URL đang chọn trong chế độ gán camera (không ảnh hưởng streamUrl của Camera 1). */
  const [assignPickedUrl, setAssignPickedUrl] = useState('');
  /** Gán RTSP cho panel đèn giao thông (màn 1 / màn 2). Rỗng = chưa chọn. */
  const [tlSelectedUrls, setTlSelectedUrls] = useState<[string, string]>(['', '']);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [roiOpen, setRoiOpen] = useState(false);
  const trimmedStream = streamUrl.trim();
  const trafficPhaseRoadLabels = useMemo(
    (): [string, string] => phaseRoadTitlesFromPrimaryUrl(trimmedStream),
    [trimmedStream],
  );
  const maxExtraPreviews = MAX_EXTRA_PREVIEW_COLS;
  const cameraOptions = useMemo(
    () => CAMERA_PRESETS.map(({ label, url }) => ({ label, url })),
    [],
  );
  const primaryCameraLabel = useMemo(() => {
    const u = trimmedStream.trim();
    if (!u) return '';
    return cameraOptions.find((c) => c.url === u)?.label ?? u;
  }, [cameraOptions, trimmedStream]);
  const previewSlots = useMemo(() => {
    const used = new Set<string>([trimmedStream.trim()]);
    const out: { label: string; url: string }[] = [];

    for (let i = 0; i < extraPreviewCount; i++) {
      const picked = (extraPreviewUrls[i] ?? '').trim();
      let url = '';
      if (picked && !used.has(picked)) url = picked;
      if (!url) break;
      used.add(url);
      const opt = cameraOptions.find((c) => c.url === url);
      out.push({ label: opt?.label ?? url, url });
    }
    return out;
  }, [trimmedStream, extraPreviewCount, extraPreviewUrls, cameraOptions]);
  const multiView = previewSlots.length > 0;

  const previewGridClass = useMemo(() => {
    const n = 1 + previewSlots.length;
    if (n <= 1) return 'grid-cols-1';
    if (n === 2) return 'grid-cols-1 lg:grid-cols-2';
    if (n === 3) return 'grid-cols-1 lg:grid-cols-2 xl:grid-cols-3';
    if (n === 4) return 'grid-cols-1 lg:grid-cols-2 xl:grid-cols-4';
    return 'grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5';
  }, [previewSlots.length]);

  const previewRefreshMs = useMemo(() => getPreviewRefreshMs(previewSlots.length), [previewSlots.length]);
  const previewThumbWidth = previewSlots.length >= 3 ? 560 : previewSlots.length >= 2 ? 640 : 720;
  // Video area should fill available space; no fixed height.

  useEffect(() => {
    setExtraPreviewCount((c) => Math.min(c, maxExtraPreviews));
  }, [maxExtraPreviews]);

  // Keep manual assignments array in sync with count
  useEffect(() => {
    setExtraPreviewUrls((prev) => {
      const next = prev.slice(0, extraPreviewCount);
      while (next.length < extraPreviewCount) next.push('');
      return next;
    });
  }, [extraPreviewCount]);

  // Merge live stats from backend with local settings for UI
  const statsForView = {
    ...stats,
    conf_threshold: settings.conf_threshold,
    line_position: settings.line_position,
  };
  const companionStatsForView = {
    ...statsForView,
    line_position: companionLinePosition,
  };

  const congestion = stats.congestion;
  const isCompanionLive = streamOn && companionActive;
  const isExtra2Live = streamOn && Boolean((extraPreviewUrls[1] ?? '').trim());
  const isExtra3Live = streamOn && Boolean((extraPreviewUrls[2] ?? '').trim());

  const urlToSlot = useCallback((urlRaw: string): string => {
    const u = (urlRaw || '').trim();
    if (!u) return 'primary';
    if (trimmedStream && u === trimmedStream) return 'primary';
    // Camera 2 LIVE is driven by extraPreviewUrls[0]
    const cam2 = (extraPreviewUrls[0] ?? '').trim();
    if (cam2 && u === cam2) return 'companion';
    const cam3 = (extraPreviewUrls[1] ?? '').trim();
    if (cam3 && u === cam3) return '2';
    const cam4 = (extraPreviewUrls[2] ?? '').trim();
    if (cam4 && u === cam4) return '3';
    // Fallback: if user picked a visible URL we can't map, treat as primary.
    return 'primary';
  }, [trimmedStream, extraPreviewUrls]);

  useEffect(() => {
    // Push mapping to backend so TLC can consume ROI+tracking from the correct camera slots.
    const p0 = urlToSlot(tlSelectedUrls[0] ?? '');
    const p1 = urlToSlot(tlSelectedUrls[1] ?? '');
    trafficLightApi.setSources({ phase0_slot: p0, phase1_slot: p1 }).catch(() => {});
  }, [tlSelectedUrls, urlToSlot]);

  useEffect(() => {
    if (!settingsOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSettingsOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [settingsOpen]);

  useEffect(() => {
    if (!cameraOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setCameraOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [cameraOpen]);

  useEffect(() => {
    if (!roiOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setRoiOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [roiOpen]);

  // H264 burn-in draws boxes on the same frame — INFERENCE_SKIP_FRAMES from .env is safe and improves FPS.

  const addToast = useCallback((message: string, type: Toast['type'] = 'info') => {
    const id = ++toastId;
    setToasts((t) => [...t, { id, message, type }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }, []);

  const openAssignFor = useCallback((idx: number) => {
    setAssignExtraIndex(idx);
    setAssignPickedUrl('');
    setCameraOpen(true);
  }, []);

  const assignToExtra = useCallback(async (idx: number, urlRaw: string) => {
    const picked = (urlRaw || '').trim();
    if (!picked) return;

    setExtraPreviewCount((c) => Math.max(c, idx + 1));
    setExtraPreviewUrls((prev) => {
      const next = prev.slice();
      while (next.length < idx + 1) next.push('');
      next[idx] = picked;
      return next;
    });

    if (idx === 0 && streamOn && trimmedStream) {
      try {
        // Start companion without restarting primary stream.
        await startCompanion(picked);
        addToast('Đã kết nối luồng Camera 2', 'success');
      } catch {
        addToast('Đã gán Camera 2 nhưng không kết nối được luồng.', 'error');
      }
    } else if ((idx === 1 || idx === 2) && picked) {
      const slot = idx + 1; // idx 1=>slot2 (màn3), idx 2=>slot3 (màn4)
      try {
        await streamApi.startExtra(slot, picked);
        addToast(`Đã kết nối luồng Camera ${idx + 2}`, 'success');
      } catch {
        addToast(`Không kết nối được luồng Camera ${idx + 2}`, 'error');
      }
    } else {
      addToast(`Đã gán Camera ${idx + 2}`, 'info');
    }

    setCameraOpen(false);
    setAssignExtraIndex(null);
    setAssignPickedUrl('');
  }, [addToast, startStream, startCompanion, streamOn, trimmedStream, streamApi]);

  // Load models list
  const reloadModels = useCallback(() => {
    modelApi.list().then(setModels).catch(() => {});
  }, []);

  // Load settings and device (GPU/CPU)
  // Reset toàn bộ backend state về mặc định khi trang load mới (F5 hoặc mở lần đầu)
  useEffect(() => {
    detectionApi.getSettings().then(setSettings).catch(() => {});
    reloadModels();
    // Dừng stream nếu vẫn còn chạy (safety net cho trường hợp beforeunload bị bỏ qua)
    streamApi.stop().catch(() => {});
    streamApi.stopCompanion().catch(() => {});
    detectionApi.clearRoi().catch(() => {});
    detectionApi.resetStats().catch(() => {});
    trafficLightApi.setAdviceEnabled(false).catch(() => {});
  }, [reloadModels]);

  useEffect(() => {
    streamApi.getDevice().then(setDeviceInfo).catch(() => {});
  }, []);

  // Khi user F5 hoặc đóng tab, stop tất cả stream đang chạy trên backend
  // Dùng sendBeacon vì fetch thông thường bị cancel trước khi gửi xong
  useEffect(() => {
    const handleUnload = () => {
      navigator.sendBeacon('/api/v1/stream/stop');
      navigator.sendBeacon('/api/v1/stream/companion/stop');
    };
    window.addEventListener('beforeunload', handleUnload);
    return () => window.removeEventListener('beforeunload', handleUnload);
  }, []);

  // Stream connect
  const handleConnect = async () => {
    const url = streamUrl.trim();
    if (!url) { addToast('Nhap URL hoac duong dan video', 'error'); return; }
    if (/youtube\.com\/watch\?v=|youtu\.be\//i.test(url)) {
      const idMatch = url.match(/(?:watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{10,15})/i);
      if (!idMatch) {
        addToast('Link YouTube khong hop le. Kiem tra video ID', 'error');
        return;
      }
    }
    setConnecting(true);
    try {
      await startStream(url);
      setStreamOn(true);
      addToast('Stream dang ket noi...', 'info');
      const deadline = Date.now() + 35000;
      const t = setInterval(async () => {
        if (Date.now() > deadline) {
          clearInterval(t);
          setStreamOn(false);
          addToast('Khong the khoi dong stream.', 'error');
          return;
        }
        try {
          const status = await streamApi.getStatus();
          if (status.error) {
            clearInterval(t);
            addToast(status.error, 'error');
            setStreamOn(false);
          } else if (status.active) {
            clearInterval(t);
          }
        } catch {
          // ignore
        }
      }, 1500);
    } catch (e: any) {
      addToast(e.message || 'Khong the ket noi stream', 'error');
    } finally {
      setConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    await stopStream();
    setStreamOn(false);
    addToast('Stream da dung', 'info');
  };

  const getCanvasRefFor = useCallback((slot: RoiSlotKey) => {
    if (slot === 'primary') return roiCanvasPrimaryRef;
    if (slot === 'companion') return roiCanvasCompanionRef;
    if (slot === 2) return roiCanvasExtra2Ref;
    return roiCanvasExtra3Ref;
  }, []);

  const getVideoFor = useCallback((slot: RoiSlotKey): HTMLVideoElement | null => {
    const canvas = getCanvasRefFor(slot).current;
    const root = canvas?.closest('.relative');
    return root?.querySelector('video') ?? null;
  }, [getCanvasRefFor]);

  function mapPointsToVideo(
    points: number[][],
    canvas: HTMLCanvasElement | null,
    video: HTMLVideoElement | null,
  ) {
    if (!canvas) return points;
    const vw = video?.videoWidth ?? 1280;
    const vh = video?.videoHeight ?? 720;
    if (!(vw > 0 && vh > 0)) return points;

    const cw = canvas.offsetWidth;
    const ch = canvas.offsetHeight;
    if (!(cw > 0 && ch > 0)) return points;

    const imgRatio = vw / vh;
    const canRatio = cw / ch;
    let dw: number, dh: number, dx: number, dy: number;
    if (imgRatio > canRatio) {
      dw = cw; dh = cw / imgRatio; dx = 0; dy = (ch - dh) / 2;
    } else {
      dh = ch; dw = ch * imgRatio; dx = (cw - dw) / 2; dy = 0;
    }
    const scaleX = vw / dw;
    const scaleY = vh / dh;
    return points.map(([x, y]) => [
      Math.round((x - dx) * scaleX),
      Math.round((y - dy) * scaleY),
    ]);
  }

  const handleApplyRoiFor = useCallback(async (slot: RoiSlotKey, canvasPoints: number[][]) => {
    const canvas = getCanvasRefFor(slot).current;
    const video = getVideoFor(slot);
    const videoPoints = mapPointsToVideo(canvasPoints, canvas, video);
    if (slot === 'primary') await setRoi(videoPoints);
    else await setRoiSlot(slot, videoPoints);

    setRoiBySlot((prev) => ({ ...prev, [String(slot)]: { ...prev[String(slot)], active: true, drawing: false } }));
    addToast(`ROI (Camera ${slot === 'primary' ? 1 : slot === 'companion' ? 2 : slot === 2 ? 3 : 4}) đã áp dụng (${canvasPoints.length} điểm)`, 'success');
  }, [addToast, getCanvasRefFor, getVideoFor, setRoi, setRoiSlot]);

  const handleClearRoiFor = useCallback(async (slot: RoiSlotKey) => {
    if (slot === 'primary') await clearRoi();
    else await clearRoiSlot(slot);
    setRoiBySlot((prev) => ({ ...prev, [String(slot)]: { active: false, points: [], drawing: false } }));
    addToast(`ROI (Camera ${slot === 'primary' ? 1 : slot === 'companion' ? 2 : slot === 2 ? 3 : 4}) đã xoá`, 'info');
  }, [addToast, clearRoi, clearRoiSlot]);

  const roiState = roiBySlot[String(roiTarget)] ?? { active: false, points: [], drawing: false };

  const roiActiveSlots = useMemo(() => {
    const slots: RoiSlotKey[] = [];
    if (streamOn && trimmedStream) slots.push('primary');
    if (streamOn && companionActive) slots.push('companion');
    if (isExtra2Live) slots.push(2);
    if (isExtra3Live) slots.push(3);
    // If nothing is live yet, still allow configuring Camera 1.
    if (slots.length === 0) slots.push('primary');
    return slots;
  }, [streamOn, trimmedStream, companionActive, isExtra2Live, isExtra3Live]);

  useEffect(() => {
    // Keep roiTarget valid when camera slots change
    if (!roiActiveSlots.includes(roiTarget)) setRoiTarget(roiActiveSlots[0] ?? 'primary');
  }, [roiActiveSlots, roiTarget]);

  // Export CSV
  const handleExport = async () => {
    const s = stats;
    const rows = [
      ['Metric', 'Value'],
      ['Total', s.total],
      ['IN (top->bottom)', s.count_in ?? 0],
      ['OUT (bottom->top)', s.count_out ?? 0],
      ['FPS', s.fps],
      ['Model', s.model_name],
      ['---', '---'],
      ['Class', 'Total', 'IN', 'OUT'],
      ...Object.entries(s.classes).map(([cls, count]) => [
        cls, count, s.classes_in?.[cls] ?? 0, s.classes_out?.[cls] ?? 0,
      ]),
    ];
    const csv = rows.map((r) => r.join(',')).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = `stats_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`;
    a.click();
    addToast('CSV da xuat', 'success');
  };

  // Settings sliders
  const handleSettingChange = (key: keyof Settings, value: number | string) => {
    setSettings((s) => ({ ...s, [key]: value }));
    updateSettings({ [key]: value } as Partial<Settings>);
  };

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-bg-base font-sans">

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <header className="flex items-center gap-3 px-5 min-h-14 py-2 bg-white border-b border-slate-200 shadow-sm shrink-0 z-50">
        {/* Left: model + device */}
        <div className="flex items-center gap-2 shrink-0">
          <StatusPill label={stats.model_loaded ? stats.model_name.replace('.pt', '') : 'No Model'} active={stats.model_loaded} />
          <StatusPill label={deviceInfo.cuda_available ? 'GPU' : 'CPU'} active={deviceInfo.cuda_available} title={deviceInfo.device_name ?? undefined} />
          <StatusPill label="H264" active={streamOn} title="Video: H264 NVENC (box burn-in)" />
          <button
            type="button"
            onClick={() => setCameraOpen(true)}
            className="h-8 px-2.5 inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 text-slate-600 hover:text-accent transition-colors shadow-sm"
            title="Camera / Stream"
            aria-label="Camera / Stream"
          >
            <IconCameraCctv className="h-4 w-4 shrink-0" aria-hidden />
            <span className="text-[11px] font-bold hidden sm:inline">Camera</span>
          </button>
        </div>

        {/* Center: system title */}
        <div className="flex-1 min-w-0 flex justify-center">
          <div className="min-w-0 text-center">
            <h1 className="text-lg sm:text-xl font-black text-accent tracking-tight truncate">
              Hệ thống giám sát giao thông
            </h1>
          </div>
        </div>

        {/* Right: FPS + perf breakdown + WS status */}
        <div className="flex items-center gap-3 text-[11px] text-slate-500 shrink-0">
          <span className="text-2xl font-bold text-accent tabular-nums">{stats.fps.toFixed(1)}</span>
          <span>FPS</span>
          <span>·</span>
          <span>Frame {stats.frame_count.toLocaleString()}</span>
          <span className="hidden md:inline">·</span>
          <span
            className="hidden md:inline text-slate-500 tabular-nums"
            title="Capture / Inference / Sent FPS"
          >
            C/I/S: {stats.fps_capture.toFixed(1)}/{stats.fps_inference.toFixed(1)}/{stats.fps_sent.toFixed(1)}
          </span>
          <span className="hidden lg:inline">·</span>
          <span className="hidden lg:inline text-slate-500 tabular-nums" title="Average YOLO inference time (ms)">
            Infer: {stats.avg_inference_ms.toFixed(1)}ms
          </span>
          {congestion && congestion.is_congested && (
            <>
              <span className="shrink-0">·</span>
              <div
                className={`
                  shrink-0 flex items-center gap-1.5 px-2.5 py-1 rounded-lg border shadow-md
                  text-[11px] font-bold max-w-[min(18rem,calc(100vw-14rem))] sm:max-w-[min(22rem,calc(100vw-16rem))]
                  animate-bounce
                  ${congestion.level === 'critical'
                    ? 'bg-red-600 text-white border-red-400 shadow-red-500/30'
                    : 'bg-amber-500 text-white border-amber-300 shadow-amber-500/30'}
                `}
                title={
                  congestion.level === 'critical'
                    ? `Kẹt xe nghiêm trọng: ${congestion.vehicle_count} xe trong ${congestion.duration_seconds.toFixed(0)}s`
                    : `Mật độ cao: ${congestion.vehicle_count} xe trong ${congestion.duration_seconds.toFixed(0)}s`
                }
              >
                <span className="shrink-0" aria-hidden>
                  {congestion.level === 'critical' ? '🚨' : '⚠️'}
                </span>
                <span className="truncate">
                  {congestion.level === 'critical' ? 'KẸT XE NGHIÊM TRỌNG' : 'MẬT ĐỘ CAO'}
                  {' — '}
                  {congestion.vehicle_count} xe / {congestion.duration_seconds.toFixed(0)}s
                </span>
              </div>
            </>
          )}
          {streamOn && (
            <>
              <span>·</span>
              {wsConnected ? (
                <span className="flex items-center gap-1 text-emerald-600 font-semibold" title="WebSocket stats/detections">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                  WS
                </span>
              ) : (
                <span className="flex items-center gap-1 text-slate-400" title="Đang kết nối WebSocket...">
                  <span className="w-1.5 h-1.5 rounded-full bg-slate-400 animate-pulse" />
                  Connecting...
                </span>
              )}
            </>
          )}
        </div>
      </header>


      {/* ── Body ───────────────────────────────────────────────────────────── */}
      <div className="flex flex-1 min-h-0">

        {/* ── Main ─────────────────────────────────────────────────────────── */}
        <main className="flex flex-1 min-w-0 min-h-0 overflow-hidden">

          {/* Video panel — 1 màn chính; mỗi lần « Mở rộng » thêm một cột preview */}
          <div className="flex flex-col flex-1 min-w-0 p-3 gap-2 min-h-0">
            {/* Multi-view controls */}
            {streamOn ? (
              <div className="flex items-center justify-between gap-2 px-0.5">
                <div className="text-[10px] font-bold uppercase tracking-wide text-slate-600">
                  Hiển thị: {1 + previewSlots.length} camera
                </div>
                <div className="flex items-center gap-2">
                  {previewSlots.length > 0 && (
                    <button
                      type="button"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => setExtraPreviewCount(0)}
                      className="h-7 px-2 rounded-lg border border-slate-200 text-[11px] font-semibold text-slate-600 bg-white hover:bg-slate-50"
                      title="Về 1 camera"
                    >
                      1 camera
                    </button>
                  )}
                  <div className="inline-flex rounded-lg border border-slate-200 overflow-hidden bg-white">
                    <button
                      type="button"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => setExtraPreviewCount(Math.max(0, previewSlots.length - 1))}
                      disabled={!trimmedStream || previewSlots.length <= 0}
                      className="h-7 w-8 inline-flex items-center justify-center text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                      title="Giảm camera"
                      aria-label="Giảm camera"
                    >
                      −
                    </button>
                    <div className="h-7 w-10 inline-flex items-center justify-center text-[11px] font-bold text-slate-700 border-x border-slate-200">
                      {previewSlots.length > 0 ? `+${previewSlots.length}` : '–'}
                    </div>
                    <button
                      type="button"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => {
                        const idx = previewSlots.length;
                        if (idx >= maxExtraPreviews) return;
                        openAssignFor(idx);
                      }}
                      disabled={!trimmedStream || previewSlots.length >= maxExtraPreviews}
                      className="h-7 w-8 inline-flex items-center justify-center text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                      title={!trimmedStream ? 'Chưa có URL camera chính' : 'Thêm camera'}
                      aria-label="Thêm camera"
                    >
                      +
                    </button>
                  </div>
                </div>
              </div>
            ) : null}

            {/* Chọn camera màn phụ: dùng chung modal Camera/Stream */}
            <div className={`gap-3 flex-1 min-h-0 min-w-0 grid ${previewGridClass} items-stretch`}>
              <div className="flex flex-col min-h-0 min-w-0 gap-1.5">
                {multiView ? (
                  <div className="flex items-center justify-between px-0.5 shrink-0 gap-2">
                    <div className="min-w-0">
                      <div className="text-[10px] font-bold uppercase tracking-wide text-slate-600">Camera 1</div>
                      {primaryCameraLabel ? (
                        <div className="text-[10px] text-slate-500 truncate" title={primaryCameraLabel}>
                          {primaryCameraLabel}
                        </div>
                      ) : null}
                    </div>
                  </div>
                ) : null}
                <div className="relative overflow-hidden flex-1 min-h-0">
                  <H264LivePlayer enabled={streamOn} onSelectCamera={() => setCameraOpen(true)} />
                  {/* Connecting overlay — visible from click Connect until stream_active=true */}
                  {streamOn && !stats.stream_active && !stats.stream_error && (
                    <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-slate-900/75 backdrop-blur-sm pointer-events-none">
                      <svg className="h-10 w-10 text-white animate-spin" viewBox="0 0 24 24" fill="none">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                        <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                      </svg>
                      <span className="text-sm font-semibold text-white tracking-wide">Đang kết nối camera…</span>
                      <span className="text-xs text-white/60">Vui lòng chờ</span>
                    </div>
                  )}
                  <RoiCanvasOverlay
                    points={roiBySlot.primary.points}
                    setPoints={(p) => setRoiBySlot((prev) => ({ ...prev, primary: { ...prev.primary, points: typeof p === 'function' ? (p as any)(prev.primary.points) : p } }))}
                    isDrawing={roiBySlot.primary.drawing}
                    setIsDrawing={(v) => setRoiBySlot((prev) => ({ ...prev, primary: { ...prev.primary, drawing: v } }))}
                    onApply={(pts) => void handleApplyRoiFor('primary', pts)}
                    onClear={() => void handleClearRoiFor('primary')}
                    active={roiBySlot.primary.active}
                    canvasRefExternal={roiCanvasPrimaryRef}
                  />
                </div>
              </div>

              {previewSlots.map((slot, i) => (
                <div key={slot.url} className="flex flex-col flex-1 min-h-0 min-w-0 gap-1.5">
                  <div className="flex items-center justify-between px-0.5 shrink-0 gap-2">
                    {(() => {
                      const manualPicked = (extraPreviewUrls[i] ?? '').trim();
                      const isManual = manualPicked.length > 0 && manualPicked === slot.url;
                      const isManualLive2 = isManual && i === 0; // màn 2 có thể chạy companion LIVE
                      const extraSlot = i === 1 ? 2 : i === 2 ? 3 : 0;
                      const isExtraLive = extraSlot > 0 && Boolean(extraLive?.[extraSlot]?.active);
                      return (
                    <div className="min-w-0">
                      <div className="text-[10px] font-bold uppercase tracking-wide text-slate-600">
                        Camera {i + 2}
                      </div>
                      <div className="text-[10px] text-slate-500 truncate" title={slot.label}>
                        {slot.label}
                      </div>
                    </div>
                      );
                    })()}
                  </div>
                  {(() => {
                    const manualPicked = (extraPreviewUrls[i] ?? '').trim();
                    const isManual = manualPicked.length > 0 && manualPicked === slot.url;
                    const isManualLive2 = isManual && i === 0;
                    const extraSlot = i === 1 ? 2 : i === 2 ? 3 : 0;
                    const live = extraSlot > 0 ? extraLive?.[extraSlot] : null;
                    if (isManualLive2) {
                      return (
                    <div className="relative overflow-hidden flex-1 min-h-0">
                      <H264LivePlayer enabled={streamOn && companionActive} wsPath="/ws/stream-h264/companion" />
                      {streamOn && companionActive && !companionStreamActive && (
                        <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-slate-900/75 backdrop-blur-sm pointer-events-none">
                          <svg className="h-10 w-10 text-white animate-spin" viewBox="0 0 24 24" fill="none">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                            <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                          </svg>
                          <span className="text-sm font-semibold text-white tracking-wide">Đang kết nối camera…</span>
                          <span className="text-xs text-white/60">Vui lòng chờ</span>
                        </div>
                      )}
                      <RoiCanvasOverlay
                        points={roiBySlot.companion.points}
                        setPoints={(p) => setRoiBySlot((prev) => ({ ...prev, companion: { ...prev.companion, points: typeof p === 'function' ? (p as any)(prev.companion.points) : p } }))}
                        isDrawing={roiBySlot.companion.drawing}
                        setIsDrawing={(v) => setRoiBySlot((prev) => ({ ...prev, companion: { ...prev.companion, drawing: v } }))}
                        onApply={(pts) => void handleApplyRoiFor('companion', pts)}
                        onClear={() => void handleClearRoiFor('companion')}
                        active={roiBySlot.companion.active}
                        canvasRefExternal={roiCanvasCompanionRef}
                      />
                    </div>
                      );
                    }
                    if (extraSlot > 0 && (extraSlot === 2 ? isExtra2Live : isExtra3Live)) {
                      return (
                    <div className="relative overflow-hidden flex-1 min-h-0">
                      <H264LivePlayer
                        enabled={streamOn}
                        wsPath={extraSlot === 2 ? '/ws/stream-h264/extra2' : '/ws/stream-h264/extra3'}
                      />
                      {streamOn && !live?.active && (
                        <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-slate-900/75 backdrop-blur-sm pointer-events-none">
                          <svg className="h-10 w-10 text-white animate-spin" viewBox="0 0 24 24" fill="none">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                            <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                          </svg>
                          <span className="text-sm font-semibold text-white tracking-wide">Đang kết nối camera…</span>
                          <span className="text-xs text-white/60">Vui lòng chờ</span>
                        </div>
                      )}
                      <RoiCanvasOverlay
                        points={(extraSlot === 2 ? roiBySlot['2'] : roiBySlot['3']).points}
                        setPoints={(p) => setRoiBySlot((prev) => {
                          const key = String(extraSlot) as '2' | '3';
                          const cur = prev[key];
                          const nextPoints = typeof p === 'function' ? (p as any)(cur.points) : p;
                          return { ...prev, [key]: { ...cur, points: nextPoints } };
                        })}
                        isDrawing={(extraSlot === 2 ? roiBySlot['2'] : roiBySlot['3']).drawing}
                        setIsDrawing={(v) => setRoiBySlot((prev) => {
                          const key = String(extraSlot) as '2' | '3';
                          return { ...prev, [key]: { ...prev[key], drawing: v } };
                        })}
                        onApply={(pts) => void handleApplyRoiFor(extraSlot as 2 | 3, pts)}
                        onClear={() => void handleClearRoiFor(extraSlot as 2 | 3)}
                        active={(extraSlot === 2 ? roiBySlot['2'] : roiBySlot['3']).active}
                        canvasRefExternal={extraSlot === 2 ? roiCanvasExtra2Ref : roiCanvasExtra3Ref}
                      />
                    </div>
                      );
                    }
                    return (
                    <div className="overflow-hidden flex-1 min-h-0">
                      <TwinSiblingPanel
                        url={slot.url}
                        label={slot.label}
                        showHeader={false}
                        className="h-full flex flex-col min-h-0"
                        refreshMs={previewRefreshMs}
                        staggerMs={getPreviewStaggerMs(i)}
                        thumbMaxWidth={previewThumbWidth}
                      />
                    </div>
                    );
                  })()}
                </div>
              ))}
            </div>
          </div>

          {/* Stats + Traffic Light panel (right sidebar) */}
          <aside className="w-64 shrink-0 border-l border-slate-200 overflow-y-auto p-3 bg-white flex flex-col gap-3">
            <TrafficLightPanel
              streamActive={streamOn}
              activeUrls={[trimmedStream, ...previewSlots.map((s) => s.url)].filter(Boolean)}
              cameraOptions={cameraOptions}
              selectedUrls={tlSelectedUrls}
              onSelectUrl={(idx, url) =>
                setTlSelectedUrls((prev) => (idx === 0 ? [url, prev[1]] : [prev[0], url]))
              }
            />

            <div className="rounded-xl border border-slate-200 bg-white p-2 shrink-0">
              <div className="grid grid-cols-3 gap-2">
                <button
                  type="button"
                  onClick={() => setCameraOpen(true)}
                  className="h-10 w-full inline-flex flex-col items-center justify-center gap-0.5 rounded-lg border border-accent/30 bg-accent/5 hover:bg-accent/10 transition-colors text-accent"
                  title="Camera / Stream"
                  aria-label="Camera / Stream"
                >
                  <IconCameraCctv className="h-[1.125rem] w-[1.125rem]" aria-hidden />
                  <span className="text-[9px] font-bold leading-none">Camera</span>
                </button>
                <button
                  type="button"
                  onClick={() => setRoiOpen(true)}
                  className="h-10 w-full inline-flex items-center justify-center rounded-lg border border-slate-200 bg-white hover:bg-slate-50 transition-colors text-slate-500 hover:text-accent"
                  title="ROI"
                  aria-label="ROI"
                >
                  <IconRoiFrame className="h-[1.125rem] w-[1.125rem]" aria-hidden />
                </button>
                <button
                  type="button"
                  onClick={() => setSettingsOpen(true)}
                  className="h-10 w-full inline-flex items-center justify-center rounded-lg border border-slate-200 bg-white hover:bg-slate-50 transition-colors shadow-sm text-slate-500 hover:text-accent"
                  title="Cài đặt"
                  aria-label="Cài đặt"
                >
                  <IconTune className="h-[1.125rem] w-[1.125rem]" aria-hidden />
                </button>
              </div>
            </div>

            <div className="flex items-center justify-between">
              <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500">Thống kê</h2>
              <span className={`w-2 h-2 rounded-full ${streamOn ? 'bg-accent animate-pulse' : 'bg-slate-300'}`} />
            </div>
            <CounterPanel
              stats={countingEnabled ? statsForView : { ...statsForView, total: 0, classes: {} }}
              onReset={resetCount}
              onExport={handleExport}
            />
            <hr className="border-slate-100" />
          </aside>

        </main>
      </div>

      {/* ── Toast Container ───────────────────────────────────────────────── */}
      <div className="fixed top-16 right-4 z-[9999] flex flex-col gap-2 pointer-events-none">
        {toasts.map((t) => (
          <ToastItem key={t.id} {...t} />
        ))}
      </div>

      {/* ── ROI Modal ────────────────────────────────────────────────────── */}
      {roiOpen ? (
        <div className="fixed inset-0 z-[9996]">
          <button
            type="button"
            className="absolute inset-0 bg-slate-900/35"
            onClick={() => setRoiOpen(false)}
            aria-label="Đóng"
          />
          <div className="absolute inset-0 flex items-start justify-center p-4 pt-20">
            <div className="w-[min(38rem,calc(100vw-2rem))] max-h-[min(80vh,42rem)] rounded-2xl border border-slate-200 bg-white shadow-2xl overflow-hidden flex flex-col">
              <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-slate-100">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <IconRoiFrame className="h-5 w-5 shrink-0 text-accent" aria-hidden />
                    <span className="text-sm font-extrabold text-slate-800 truncate">ROI</span>
                  </div>
                  <div className="text-[11px] text-slate-500 truncate">Vẽ vùng quan tâm trên khung video để lọc/đếm.</div>
                </div>
                <button
                  type="button"
                  onClick={() => setRoiOpen(false)}
                  className="shrink-0 px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                >
                  Đóng
                </button>
              </div>
              <div className="p-4 overflow-auto">
                <div className="mb-3 flex flex-wrap gap-2">
                  {roiActiveSlots.map((k) => {
                    const label = k === 'primary' ? 'Camera 1' : k === 'companion' ? 'Camera 2' : k === 2 ? 'Camera 3' : 'Camera 4';
                    const isOn = roiTarget === k;
                    return (
                      <button
                        key={String(k)}
                        type="button"
                        onClick={() => setRoiTarget(k)}
                        className={`px-3 py-1.5 rounded-lg border text-xs font-semibold transition-colors ${
                          isOn ? 'bg-blue-50 border-accent text-accent' : 'bg-white border-slate-200 text-slate-600 hover:border-slate-300'
                        }`}
                      >
                        {label}
                      </button>
                    );
                  })}
                </div>
                <RoiDrawer
                  onApply={(pts) => void handleApplyRoiFor(roiTarget, pts)}
                  onClear={() => void handleClearRoiFor(roiTarget)}
                  active={roiState.active}
                  points={roiState.points}
                  setPoints={(p) => setRoiBySlot((prev) => {
                    const key = String(roiTarget);
                    const cur = prev[key] ?? { active: false, points: [], drawing: false };
                    const nextPoints = typeof p === 'function' ? (p as any)(cur.points) : p;
                    return { ...prev, [key]: { ...cur, points: nextPoints } };
                  })}
                  isDrawing={roiState.drawing}
                  setIsDrawing={(v) => setRoiBySlot((prev) => {
                    const key = String(roiTarget);
                    const cur = prev[key] ?? { active: false, points: [], drawing: false };
                    return { ...prev, [key]: { ...cur, drawing: v } };
                  })}
                />
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {/* ── Camera Modal ─────────────────────────────────────────────────── */}
      {cameraOpen ? (
        <div className="fixed inset-0 z-[9997]">
          <button
            type="button"
            className="absolute inset-0 bg-slate-900/35"
            onClick={() => { setCameraOpen(false); setAssignExtraIndex(null); }}
            aria-label="Đóng"
          />
          <div className="absolute inset-0 flex items-start justify-center p-4 pt-16">
            <div className="w-[min(48rem,calc(100vw-2rem))] max-h-[min(85vh,48rem)] rounded-2xl border border-slate-200 bg-white shadow-2xl overflow-hidden flex flex-col">
              <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-slate-100">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <IconCameraCctv className="h-5 w-5 shrink-0 text-accent" aria-hidden />
                    <span className="text-sm font-extrabold text-slate-800 truncate">Camera / Stream</span>
                  </div>
                  {assignExtraIndex != null ? (
                    <div className="text-[11px] text-slate-500 truncate">
                      Đang chọn cho <span className="font-semibold text-slate-700">Camera {assignExtraIndex + 2}</span> — double-click để gán camera.
                    </div>
                  ) : (
                    <div className="text-[11px] text-slate-500 truncate">Chọn camera preset hoặc nhập RTSP/video → Connect.</div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => { setCameraOpen(false); setAssignExtraIndex(null); }}
                  className="shrink-0 px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                >
                  Đóng
                </button>
              </div>

              <div className="p-4 overflow-auto space-y-4">
                <CameraWall
                  cameras={CAMERA_PRESETS}
                  selectedUrl={assignExtraIndex != null ? assignPickedUrl : streamUrl}
                  activeUrl={trimmedStream}
                  onSelect={(url) => {
                    if (assignExtraIndex != null) setAssignPickedUrl(url);
                    else setStreamUrl(url);
                  }}
                  onConnect={async (url) => {
                    if (!url.trim()) return;
                    // Assign mode: gán camera cho màn phụ, không connect stream chính
                    if (assignExtraIndex != null) {
                      await assignToExtra(assignExtraIndex, url);
                      return;
                    }

                    setStreamUrl(url);
                    setConnecting(true);
                    try {
                      if (streamOn) {
                        await stopStream();
                        setStreamOn(false);
                        await new Promise(r => setTimeout(r, 500));
                      }
                      const turl = url.trim();
                      await startStream(turl);
                      setStreamOn(true);
                      addToast(`Dang ket noi: ${turl.split('/').pop()}`, 'info');
                      const deadline = Date.now() + 35000;
                      const t = setInterval(async () => {
                        if (Date.now() > deadline) { clearInterval(t); setStreamOn(false); addToast('Timeout ket noi', 'error'); return; }
                        try {
                          const status = await streamApi.getStatus();
                          if (status.active) clearInterval(t);
                          else if (status.error) { clearInterval(t); setStreamOn(false); addToast(status.error, 'error'); }
                        } catch { /* ignore */ }
                      }, 1500);
                    } catch (e: any) {
                      addToast(e.message || 'Khong the ket noi', 'error');
                    } finally {
                      setConnecting(false);
                    }
                  }}
                  streamOn={streamOn}
                  connecting={connecting}
                />

                <div className="flex flex-col gap-2">
                  <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold">Hoặc nhập URL thủ công</p>
                  <input
                    type="text"
                    value={assignExtraIndex != null ? assignPickedUrl : streamUrl}
                    onChange={(e) => {
                      if (assignExtraIndex != null) setAssignPickedUrl(e.target.value);
                      else setStreamUrl(e.target.value);
                    }}
                    onKeyDown={(e) => {
                      if (e.key !== 'Enter') return;
                      if (assignExtraIndex != null) assignToExtra(assignExtraIndex, assignPickedUrl);
                      else handleConnect();
                    }}
                    placeholder="rtsp://... hoặc C:/path/video.mp4"
                    className="w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg text-slate-800 placeholder-slate-400 outline-none focus:border-accent focus:ring-1 focus:ring-accent/30 transition-colors"
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={() => {
                        if (assignExtraIndex != null) assignToExtra(assignExtraIndex, assignPickedUrl);
                        else handleConnect();
                      }}
                      disabled={connecting || streamOn}
                      className="flex-1 py-2 text-xs font-bold rounded-lg bg-accent text-white disabled:opacity-40 hover:bg-blue-700 transition-all"
                    >
                      {connecting ? 'Connecting...' : assignExtraIndex != null ? '➕ Thêm màn' : '▶ Connect'}
                    </button>
                    <button
                      onClick={handleDisconnect}
                      disabled={!streamOn}
                      className="flex-1 py-2 text-xs font-semibold rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-100 disabled:opacity-30 transition-all"
                    >
                      ■ Stop
                    </button>
                  </div>
                </div>

                {trimmedStream ? (
                  <p className="text-[10px] text-slate-500 leading-snug">
                    <span className="font-semibold text-slate-600">Mở rộng thêm màn</span> dưới khung video để thêm từng
                    màn, rồi chọn camera trong danh sách. Thu gọn từng màn một.
                  </p>
                ) : null}
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {/* ── Settings Modal ───────────────────────────────────────────────── */}
      {settingsOpen ? (
        <div className="fixed inset-0 z-[9998]">
          <button
            type="button"
            className="absolute inset-0 bg-slate-900/35"
            onClick={() => setSettingsOpen(false)}
            aria-label="Đóng"
          />
          <div className="absolute inset-0 flex items-start justify-center p-4 pt-20">
            <div className="w-[min(42rem,calc(100vw-2rem))] max-h-[min(80vh,42rem)] rounded-2xl border border-slate-200 bg-white shadow-2xl overflow-hidden flex flex-col">
              <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-slate-100">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <IconTune className="h-5 w-5 shrink-0 text-accent" aria-hidden />
                    <span className="text-sm font-extrabold text-slate-800 truncate">Cài đặt</span>
                  </div>
                  <div className="text-[11px] text-slate-500 truncate">Chỉnh thông số nhận diện, đếm xe và cảnh báo kẹt xe.</div>
                </div>
                <button
                  type="button"
                  onClick={() => setSettingsOpen(false)}
                  className="shrink-0 px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                >
                  Đóng
                </button>
              </div>

              <div className="p-4 overflow-auto">
                <div className="rounded-xl border border-slate-200 bg-white p-3 mb-4">
                  <div className="text-[11px] font-bold text-slate-600 uppercase tracking-wide mb-2">Export TensorRT</div>
                  <ModelUploader
                    models={models}
                    onModelsChange={reloadModels}
                    onToast={addToast}
                    onReloadStats={reloadStats}
                    cudaAvailable={deviceInfo.cuda_available}
                  />
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div className="rounded-xl border border-slate-200 bg-white p-3">
                    <div className="text-[11px] font-bold text-slate-600 uppercase tracking-wide mb-2">Nhận diện</div>
                    <SliderField
                      label="Confidence"
                      value={settings.conf_threshold}
                      min={0.1} max={0.95} step={0.01}
                      display={settings.conf_threshold.toFixed(2)}
                      onChange={(v) => handleSettingChange('conf_threshold', v)}
                      onCommit={(v) => addToast(`Confidence: ${v.toFixed(2)}`, 'info')}
                    />
                    <SliderField
                      label="Counting Line"
                      value={settings.line_position}
                      min={0.1} max={0.9} step={0.01}
                      display={`${Math.round(settings.line_position * 100)}%`}
                      onChange={(v) => handleSettingChange('line_position', v)}
                      onCommit={(v) => addToast(`Counting Line: ${Math.round(v * 100)}%`, 'info')}
                    />
                    {roiBySlot.primary?.active && (
                      <p className="text-[10px] text-amber-600 mt-0.5 mb-1">
                        ROI đang bật — đường đếm tự động theo giữa ROI, slider không có tác dụng.
                      </p>
                    )}
                    <SliderField
                      label="Inference Skip Frames"
                      value={settings.skip_frames ?? 0}
                      min={0}
                      max={5}
                      step={1}
                      display={`${settings.skip_frames ?? 0}`}
                      onChange={(v) => handleSettingChange('skip_frames', v)}
                      onCommit={(v) => addToast(
                        v === 0 ? 'Skip Frames: tắt (infer mỗi frame)' : `Skip Frames: ${v} (infer 1/${v + 1} frame)`,
                        'info'
                      )}
                    />
                  </div>

                  <div className="rounded-xl border border-slate-200 bg-white p-3">
                    <div className="text-[11px] font-bold text-slate-600 uppercase tracking-wide mb-2">Đếm xe</div>

                    <div className="mb-3">
                      <span className="text-[11px] text-slate-500 block mb-1">Tracker</span>
                      <select
                        value={settings.tracker_type ?? 'bytetrack'}
                        onChange={(e) => {
                          const v = e.target.value;
                          setSettings((s) => ({ ...s, tracker_type: v }));
                          updateSettings({ tracker_type: v });
                          const names: Record<string, string> = { bytetrack: 'ByteTrack', botsort: 'BoT-SORT' };
                          addToast(`Tracker: ${names[v] ?? v}`, 'success');
                        }}
                        className="w-full text-xs border border-slate-300 rounded-lg px-2 py-2 bg-white text-slate-700"
                      >
                        <option value="bytetrack">ByteTrack (recommended)</option>
                        <option value="botsort">BoT-SORT</option>
                      </select>
                    </div>

                    <div className="mb-3">
                      <span className="text-[11px] text-slate-500 block mb-1">Che do dem</span>
                      <select
                        value={settings.counting_mode ?? 'all'}
                        onChange={(e) => {
                          const v = e.target.value as 'all' | 'direction';
                          setSettings((s) => ({ ...s, counting_mode: v }));
                          updateSettings({ counting_mode: v });
                          addToast(v === 'all' ? 'Chế độ: Đếm tổng hợp' : 'Chế độ: Đếm theo chiều IN / OUT', 'success');
                        }}
                        className="w-full text-xs border border-slate-300 rounded-lg px-2 py-2 bg-white text-slate-700"
                      >
                        <option value="all">Dem tat ca (tong hop)</option>
                        <option value="direction">Dem theo chieu (IN / OUT)</option>
                      </select>
                    </div>

                    <div className="flex items-center justify-between">
                      <span className="text-[11px] text-slate-500">Enable Counting</span>
                      <button
                        type="button"
                        onClick={() => {
                          const next = !countingEnabled;
                          setCountingEnabled(next);
                          addToast(next ? 'Hiển thị số đếm xe' : 'Ẩn số đếm xe (backend vẫn đếm)', 'info');
                        }}
                        className={`relative inline-flex h-4 w-8 items-center rounded-full border transition-colors ${
                          countingEnabled ? 'bg-accent border-accent' : 'bg-slate-200 border-slate-300'
                        }`}
                      >
                        <span
                          className={`inline-block h-3 w-3 rounded-full bg-white shadow transform transition-transform ${
                            countingEnabled ? 'translate-x-4' : 'translate-x-1'
                          }`}
                        />
                      </button>
                    </div>
                  </div>
                </div>

                <div className="mt-4 rounded-xl border border-slate-200 bg-white p-3">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-[11px] font-bold text-slate-600 uppercase tracking-wide">Cảnh báo kẹt xe</span>
                    <span title="Cảnh báo mật độ / kẹt xe" className="inline-flex shrink-0 text-amber-500">
                      <IconAlertJam className="h-4 w-4" aria-hidden />
                    </span>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <SliderField
                      label="Nguong phuong tien"
                      value={settings.congestion_threshold ?? 10}
                      min={1} max={50} step={1}
                      display={`${settings.congestion_threshold ?? 10}`}
                      onChange={(v) => handleSettingChange('congestion_threshold', v)}
                    />
                    <SliderField
                      label="Thoi gian on dinh (s)"
                      value={settings.congestion_duration ?? 5}
                      min={1} max={60} step={1}
                      display={`${settings.congestion_duration ?? 5}s`}
                      onChange={(v) => handleSettingChange('congestion_duration', v)}
                    />
                  </div>
                  {congestion && (
                    <div className={`mt-2 px-3 py-2 rounded-lg text-xs font-semibold ${
                      congestion.level === 'critical' ? 'bg-red-100 text-red-700 border border-red-200' :
                      congestion.level === 'warning' ? 'bg-amber-100 text-amber-700 border border-amber-200' :
                      'bg-green-50 text-green-700 border border-green-200'
                    }`}>
                      {congestion.level === 'normal'
                        ? `Binh thuong (${congestion.vehicle_count} xe)`
                        : `${congestion.level === 'critical' ? 'Nghiem trong' : 'Canh bao'}: ${congestion.vehicle_count} xe / ${congestion.duration_seconds.toFixed(0)}s`
                      }
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StatusPill({
  label,
  active,
  title,
  onClick,
}: {
  label: string;
  active: boolean;
  title?: string;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-[11px] font-semibold border transition-all ${
        active ? 'border-accent/40 text-accent bg-blue-50' : 'border-slate-200 text-slate-500 bg-slate-50'
      } ${onClick ? 'cursor-pointer hover:brightness-95' : 'cursor-default'}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${active ? 'bg-accent animate-pulse' : 'bg-slate-300'}`} />
      {label}
    </button>
  );
}

function SideCard({ title, icon, children }: { title: string; icon: string; children: ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm hover:border-slate-300 transition-colors">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 w-full px-3 py-2.5 text-left hover:bg-slate-50 transition-colors"
      >
        <span className="text-sm">{icon}</span>
        <span className="flex-1 text-xs font-semibold text-slate-800">{title}</span>
        <span className={`text-slate-400 text-xs transition-transform ${open ? '' : '-rotate-90'}`}>▾</span>
      </button>
      {open && <div className="px-3 pb-3">{children}</div>}
    </div>
  );
}

function SliderField({ label, value, min, max, step, display, onChange, onCommit }: {
  label: string; value: number; min: number; max: number; step: number;
  display: string; onChange: (v: number) => void; onCommit?: (v: number) => void;
}) {
  return (
    <div className="mb-3 last:mb-0">
      <div className="flex justify-between items-center mb-1.5">
        <span className="text-[11px] text-slate-500">{label}</span>
        <span className="text-[11px] font-bold text-accent tabular-nums">{display}</span>
      </div>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        onPointerUp={(e) => onCommit?.(parseFloat((e.target as HTMLInputElement).value))}
        className="w-full h-1.5 appearance-none bg-slate-200 rounded-full cursor-pointer accent-accent"
        style={{ accentColor: '#2563eb' }}
      />
    </div>
  );
}

function ToastItem({ message, type }: Toast) {
  const styles = {
    success: 'border-blue-200 bg-blue-50 text-accent',
    error:   'border-red-200 bg-red-50 text-red-600',
    info:    'border-slate-200 bg-slate-50 text-slate-700',
    warning: 'border-amber-200 bg-amber-50 text-amber-700',
  };
  const icons  = { success: '✓', error: '✕', info: 'ℹ', warning: '⚠' };
  return (
    <div className={`flex items-start gap-2.5 px-4 py-3 rounded-xl border text-xs shadow-lg pointer-events-auto animate-toastIn max-w-xs ${styles[type]}`}>
      <span className="shrink-0 mt-0.5">{icons[type]}</span>
      <span>{message}</span>
    </div>
  );
}
