import { useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react';
import { trafficLightApi } from '../../services/api';
import type { TLState } from '../../services/api';
import { CameraWall } from '../CameraWall/CameraWall';

export type TrafficLightPanelProps = {
  phaseRoadLabels?: readonly [string, string];
  /** RTSP URLs currently visible in the main camera area (Camera 1..N). */
  activeUrls?: readonly string[];
  cameraOptions?: readonly { label: string; url: string }[];
  selectedUrls?: readonly [string, string];
  onSelectUrl?: (phaseIndex: 0 | 1, url: string) => void;
  /** Luồng camera chính đang Connect — cần cập nhật "Dừng ROI". */
  streamActive?: boolean;
};

function getRtspRoot(url: string): string {
  const u = (url || '').trim();
  if (!u) return '';
  // Match: rtsp(s)://[user[:pass]@]host[:port]/
  const m = u.match(/^(rtsps?:\/\/[^/]+)\//i);
  return (m?.[1] ?? '').toLowerCase();
}

function useOnEscape(onEscape: () => void, active: boolean) {
  useEffect(() => {
    if (!active) return;
    const handler = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') onEscape();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [active, onEscape]);
}

/** Rút từ label đầy đủ → "Camera 6", "Camera 5", … */
function shortCameraLabel(raw: string, fallbackIndex: number): string {
  const s = raw.trim();
  const m = s.match(/\bcamera\s*(\d+)\b/i);
  if (m) return `Camera ${m[1]}`;
  if (s.length > 0) return s.length <= 14 ? s : `${s.slice(0, 12)}…`;
  return `Camera ${fallbackIndex + 1}`;
}

type AdviceKind = 'green' | 'yellow' | 'red';

function adviceKindForPhase(p: TLState['phases'][number]): AdviceKind {
  const k = p.advice_countdown;
  if (k === 'green' || k === 'yellow' || k === 'red') return k;
  return 'red';
}

function adviceValueForPhase(p: TLState['phases'][number]): number {
  const kind = adviceKindForPhase(p);
  if (kind === 'red') return p.red_time_hint ?? 0;
  return p.green_time ?? 0;
}

/** Hiển thị giây đếm ngược (làm tròn lên, tránh kẹt 0 sớm khi còn 0.xs). */
function adviceSecondsDisplay(sec: number): number {
  if (sec <= 0) return 0;
  return Math.ceil(sec - 1e-6);
}

type AdviceSnap = { remain: number; atMs: number; kind: AdviceKind };

/** Đồng bộ mốc đếm ngược mượt: giữ giây thực (float), chỉ chỉnh khi lệch server. */
function syncAdviceSnaps(
  snaps: [AdviceSnap, AdviceSnap],
  phases: TLState['phases'],
  showAdvice: boolean,
): void {
  if (!showAdvice) {
    snaps[0] = { remain: 0, atMs: 0, kind: 'green' };
    snaps[1] = { remain: 0, atMs: 0, kind: 'green' };
    return;
  }
  const now = Date.now();
  phases.slice(0, 2).forEach((p, idx) => {
    const kind = adviceKindForPhase(p);
    const serverRemain = Math.max(0, adviceValueForPhase(p));
    const prev = snaps[idx];
    if (prev.atMs === 0 || prev.kind !== kind) {
      const peak = Math.max(0, p.advice_peak_sec ?? 0);
      let initial = serverRemain;
      // Chỉ khi vừa chuyển xanh→vàng: server có thể gửi số nhỏ do poll trễ — không áp dụng cuối vàng (tránh nhảy 3→2→3).
      if (kind === 'yellow' && prev.kind === 'green' && peak > 0) {
        initial = Math.max(serverRemain, peak);
      }
      snaps[idx] = { remain: initial, atMs: now, kind };
      return;
    }
    const predicted = Math.max(0, prev.remain - (now - prev.atMs) / 1000);
    const drift = serverRemain - predicted;
    // Chu kỳ mới: server nhảy 0→30/33 — phải bắt lại mốc (kind vẫn 'red' trên pha phục vụ).
    const rolledOver = serverRemain > 1 && predicted <= 0.05;
    if (drift > 0.35 || predicted - serverRemain > 0.35 || rolledOver) {
      snaps[idx] = { remain: serverRemain, atMs: now, kind };
    }
  });
}

function TrafficLightVisual({
  phases,
  showAdvice,
  phaseTitles,
  adviceSnapsRef,
}: {
  phases: TLState['phases'];
  showAdvice: boolean;
  phaseTitles: readonly [string, string];
  adviceSnapsRef: MutableRefObject<[AdviceSnap, AdviceSnap]>;
}) {
  const snapsRef = adviceSnapsRef;
  const phasesRef = useRef<TLState['phases']>(phases);
  const [tick, setTick] = useState(0);
  const lastShownRef = useRef<[number, number]>([-1, -1]);

  useEffect(() => {
    phasesRef.current = phases;
    syncAdviceSnaps(snapsRef.current, phases, showAdvice);
  }, [phases, showAdvice]);

  useEffect(() => {
    if (!showAdvice) {
      lastShownRef.current = [-1, -1];
      return;
    }
    let raf = 0;
    const loop = () => {
      const now = Date.now();
      let changed = false;
      for (let idx = 0; idx < 2; idx++) {
        let snap = snapsRef.current[idx];
        const raw = Math.max(0, snap.remain - (now - snap.atMs) / 1000);

        const shown = adviceSecondsDisplay(raw);
        if (shown !== lastShownRef.current[idx]) {
          lastShownRef.current[idx] = shown;
          changed = true;
        }
      }
      if (changed) setTick((n) => n + 1);
      raf = window.requestAnimationFrame(loop);
    };
    raf = window.requestAnimationFrame(loop);
    return () => window.cancelAnimationFrame(raf);
  }, [showAdvice]);

  return (
    <div className="grid grid-cols-2 gap-3">
      {phases.slice(0, 2).map((p, idx) => {
        const color = p.color;
        const adviceKind = adviceKindForPhase(p);
        const titleFull = (phaseTitles[idx] ?? '').trim();
        const titleShort = shortCameraLabel(titleFull, idx);
        const q = p.queue_length ?? 0;

        void tick;
        const snap = snapsRef.current[idx];
        const countdownSec = showAdvice
          ? Math.max(
              0,
              adviceSecondsDisplay(
                snap.remain - (Date.now() - snap.atMs) / 1000,
              ),
            )
          : 0;
        /** Nhãn cạnh chấm tròn: giây gợi ý cố định (không giảm). Số lớn giữa đèn: đếm ngược. */
        const badgeSuggestedSec = showAdvice
          ? Math.max(0, Math.floor(p.advice_peak_sec ?? 0))
          : 0;

        /** Chấm tròn + đèn 3 màu = pha hiện tại (mô phỏng). */
        const badgeTone = showAdvice
          ? color === 'red'
            ? 'bg-red-500'
            : color === 'yellow'
              ? 'bg-amber-400'
              : 'bg-emerald-500'
          : 'bg-slate-400';

        const isFlash = Boolean(p.advice_flash);

        // Màu số đếm ngược: khi đang nháy gợi ý dùng cyan để dễ nhận biết
        const adviceNumCls = isFlash
          ? 'text-cyan-300'
          : adviceKind === 'green'
            ? 'text-emerald-400'
            : adviceKind === 'yellow'
              ? 'text-amber-300'
              : 'text-red-400';

        const suggestedLabelCls =
          adviceKind === 'green'
            ? 'text-emerald-500'
            : adviceKind === 'yellow'
              ? 'text-amber-500'
              : 'text-red-500';
        const badgeTitle =
          adviceKind === 'green'
            ? `Gợi ý xanh (G): ${badgeSuggestedSec}s`
            : adviceKind === 'yellow'
              ? `Gợi ý vàng: ${badgeSuggestedSec}s`
              : `Gợi ý đỏ (R): ${badgeSuggestedSec}s`;
        const countdownTitle =
          adviceKind === 'green'
            ? `Gợi ý xanh (G): còn ${countdownSec}s`
            : adviceKind === 'yellow'
              ? `Gợi ý vàng: còn ${countdownSec}s`
              : `Gợi ý đỏ (R): còn ${countdownSec}s`;

        return (
          <div key={idx} className="rounded-xl border border-slate-200 bg-white px-2 py-3 shadow-sm">
            <div
              className="text-[11px] font-bold text-slate-800 text-center leading-none min-h-[1.25rem] px-0.5"
              title={titleFull || titleShort}
            >
              {titleShort}
            </div>

            <div className="mt-2 flex items-center justify-center gap-3">
              <div className="flex shrink-0 flex-col items-center gap-1">
                <span
                  className={`h-5 w-5 rounded-full shadow-sm ring-2 ring-white ${badgeTone}`}
                  title={
                    showAdvice
                      ? color === 'red'
                        ? 'Pha hiện tại: đỏ'
                        : color === 'yellow'
                          ? 'Pha hiện tại: vàng'
                          : 'Pha hiện tại: xanh'
                      : 'Tắt gợi ý — không chỉ báo phase'
                  }
                  aria-hidden
                />
                {showAdvice && badgeSuggestedSec > 0 && adviceKind !== 'yellow' ? (
                  <span
                    className={`text-[11px] font-black tabular-nums leading-none ${suggestedLabelCls}`}
                    title={badgeTitle}
                  >
                    {badgeSuggestedSec}s
                  </span>
                ) : null}
              </div>
              <div className="relative bg-slate-900 rounded-2xl p-2.5 shadow-lg border border-slate-800">
                <div
                  className={`flex flex-col gap-2.5 ${showAdvice ? 'opacity-[0.32]' : 'opacity-100'}`}
                >
                  {(['red', 'yellow', 'green'] as const).map((c) => {
                    const on = showAdvice ? color === c : true;
                    const base =
                      c === 'red'
                        ? 'bg-red-500 border-red-300 shadow-[0_0_16px_rgba(239,68,68,0.55)]'
                        : c === 'yellow'
                          ? 'bg-yellow-400 border-yellow-200 shadow-[0_0_16px_rgba(250,204,21,0.55)]'
                          : 'bg-green-500 border-green-300 shadow-[0_0_16px_rgba(34,197,94,0.55)]';

                    return (
                      <div
                        key={c}
                        className={`h-10 w-10 rounded-full border-2 transition-all duration-300 ${
                          on ? base : 'bg-slate-700 border-slate-600'
                        }`}
                      />
                    );
                  })}
                </div>
                {showAdvice ? (
                  <div
                    className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-2xl"
                    title={countdownTitle}
                  >
                    <span
                      className={`font-black tabular-nums text-[28px] leading-none drop-shadow-[0_2px_10px_rgba(0,0,0,0.95)] ${adviceNumCls}`}
                    >
                      {countdownSec}
                    </span>
                  </div>
                ) : null}
              </div>
            </div>

            <div className="mt-2 text-center text-[10px] font-semibold text-slate-600 tabular-nums">
              Dừng ROI: {q}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function RtspAssignModalBody({
  options,
  selectedUrls,
  onSelectUrl,
  activeUrls,
}: {
  options: readonly { label: string; url: string }[];
  selectedUrls: readonly [string, string];
  onSelectUrl: (phaseIndex: 0 | 1, url: string) => void;
  activeUrls: readonly string[];
}) {
  const [picked, setPicked] = useState<string>('');

  const visibleOnly = useMemo(() => {
    const set = new Set((activeUrls ?? []).map((u) => u.trim()).filter(Boolean));
    if (!set.size) return [];
    return options.filter((o) => set.has(o.url.trim()));
  }, [options, activeUrls]);

  const pickedOpt = useMemo(() => visibleOnly.find((o) => o.url === picked) ?? null, [visibleOnly, picked]);

  const initialPicked = (activeUrls?.[0] ?? '').trim();
  useEffect(() => {
    setPicked(initialPicked);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPicked]);

  return (
    <div className="p-4">
      <div className="grid grid-cols-2 gap-4">
        <div className="min-w-0">
          <div className="text-[11px] font-bold text-slate-700 mb-2">Chọn camera</div>
          <div className="rounded-xl border border-slate-200 bg-white p-2">
            {visibleOnly.length ? (
              <CameraWall
                cameras={visibleOnly.map((o) => ({ id: o.url, label: o.label, location: '', url: o.url }))}
                selectedUrl={picked}
                activeUrl=""
                onSelect={setPicked}
                onConnect={(u) => setPicked(u)}
                streamOn={false}
                connecting={false}
                variant="compact"
                showHeader={false}
                showHint={false}
                columns={2}
              />
            ) : (
              <div className="text-center text-[11px] text-slate-400 py-10">
                Chưa có camera nào đang hiển thị.
              </div>
            )}
          </div>
        </div>

        <div className="min-w-0">
          <div className="flex items-center justify-between gap-2 mb-2">
            <div className="text-[11px] font-bold text-slate-700">Gán cho đèn</div>
            <div
              className="text-[11px] font-semibold text-slate-700 px-2.5 py-1 rounded-full border border-slate-200 bg-slate-50 truncate max-w-[16rem]"
              title={pickedOpt?.label ?? picked}
            >
              {pickedOpt?.label ?? (picked ? 'Đang chọn' : 'Chưa chọn')}
            </div>
          </div>

          <div className="mt-3 space-y-3">
            {([0, 1] as const).map((idx) => {
              const assignedUrl = selectedUrls[idx]?.trim() ?? '';
              const assignedOpt = options.find((o) => o.url === assignedUrl) ?? null;
              return (
                <div key={idx} className="rounded-xl border border-slate-200 bg-white p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-xs font-bold text-slate-800">Camera {idx + 1}</div>
                    <button
                      type="button"
                      onClick={() => onSelectUrl(idx, '')}
                      className="text-[10px] font-semibold text-slate-500 hover:text-slate-700"
                    >
                      Bỏ chọn
                    </button>
                  </div>

                  <div className="text-[10px] text-slate-500 mt-1 truncate" title={assignedOpt?.label ?? assignedUrl}>
                    {assignedOpt?.label ?? (assignedUrl ? assignedUrl : 'Chưa chọn')}
                  </div>

                  <button
                    type="button"
                    disabled={!picked}
                    onClick={() => onSelectUrl(idx, picked)}
                    className="mt-3 w-full px-3 py-2 rounded-lg text-xs font-bold transition-colors disabled:opacity-40 disabled:cursor-not-allowed bg-accent text-white hover:bg-accent/90"
                  >
                    Gán vào Camera {idx + 1}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

export function TrafficLightPanel({
  phaseRoadLabels,
  activeUrls,
  cameraOptions,
  selectedUrls,
  onSelectUrl,
  streamActive = false,
}: TrafficLightPanelProps = {}) {
  const [state, setState] = useState<TLState | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval>>();
  const [adviceEnabled, setAdviceEnabled] = useState(false);
  const adviceSnapsRef = useRef<[AdviceSnap, AdviceSnap]>([
    { remain: 0, atMs: 0, kind: 'green' },
    { remain: 0, atMs: 0, kind: 'green' },
  ]);

  const phaseTitles = useMemo((): [string, string] => {
    const labelFor = (i: 0 | 1): string => {
      const url = selectedUrls?.[i]?.trim() ?? '';
      if (!url) return (phaseRoadLabels?.[i] ?? '').trim();
      const opt = cameraOptions?.find((c) => c.url === url);
      const fromOpt = (opt?.label ?? '').trim();
      if (fromOpt) return fromOpt;
      return (phaseRoadLabels?.[i] ?? '').trim() || url;
    };
    return [labelFor(0), labelFor(1)];
  }, [selectedUrls, cameraOptions, phaseRoadLabels]);

  // Chỉ poll khi cần: bật gợi ý (đếm ngược) hoặc đang stream (cập nhật Dừng ROI).
  useEffect(() => {
    let dead = false;
    const poll = async () => {
      if (dead) return;
      try {
        const s = await trafficLightApi.getState();
        if (dead) return;
        if (Boolean(s.lane_density_advice?.enabled)) {
          syncAdviceSnaps(adviceSnapsRef.current, s.phases, true);
        }
        setState(s);
        setAdviceEnabled(Boolean(s.lane_density_advice?.enabled));
      } catch {
        // ignore
      }
    };

    void poll();

    const needInterval = streamActive || adviceEnabled;
    if (!needInterval) {
      return () => {
        dead = true;
      };
    }

    const intervalMs = adviceEnabled ? 100 : 2000;
    pollRef.current = setInterval(() => void poll(), intervalMs);
    return () => {
      dead = true;
      clearInterval(pollRef.current);
    };
  }, [streamActive, adviceEnabled]);

  const anyRtspSelected = Boolean(selectedUrls?.[0]?.trim() || selectedUrls?.[1]?.trim());
  const [rtspModalOpen, setRtspModalOpen] = useState(false);
  useOnEscape(() => setRtspModalOpen(false), rtspModalOpen);

  const filteredCameraOptions = useMemo(() => {
    const opts = cameraOptions ?? [];
    const set = new Set((activeUrls ?? []).map((u) => u.trim()).filter(Boolean));
    if (!set.size) return [];
    return opts.filter((o) => set.has(o.url.trim()));
  }, [cameraOptions, activeUrls]);

  return (
    <div className="flex flex-col gap-3 p-3 bg-white rounded-xl border border-slate-200 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-bold text-slate-800">Đèn giao thông</h3>
        <span
          className={`text-[10px] px-2 py-0.5 rounded-full font-semibold border ${
            anyRtspSelected ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-50 text-slate-600 border-slate-200'
          }`}
        >
          {anyRtspSelected ? 'Đang theo dõi' : 'Đang chờ'}
        </span>
      </div>

      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          disabled={!anyRtspSelected}
          onClick={async () => {
            try {
              const next = !adviceEnabled;
              const s = await trafficLightApi.setAdviceEnabled(next);
              setState(s);
              setAdviceEnabled(Boolean(s.lane_density_advice?.enabled));
            } catch {
              // ignore
            }
          }}
          className={`px-3 py-2 rounded-xl text-xs font-bold border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
            adviceEnabled ? 'bg-emerald-600 text-white border-emerald-700 hover:bg-emerald-700' : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
          }`}
          title="Bật/tắt gợi ý: ghép hai ROI (xoay hệ số) — xanh và khối đỏ tiếp theo; cần ROI trên cả 2 camera"
        >
          {adviceEnabled ? 'Tắt gợi ý' : 'Bật gợi ý'}
        </button>
        {adviceEnabled && state?.lane_density_advice?.note ? (
          <div className="text-[10px] text-amber-700 font-semibold truncate" title={state.lane_density_advice.note}>
            {state.lane_density_advice.note}
          </div>
        ) : (
          <div className="flex-1" />
        )}
      </div>

      {cameraOptions && selectedUrls && onSelectUrl ? (
        <>
          <button
            type="button"
            onClick={() => setRtspModalOpen(true)}
            className="w-full px-3 py-2 rounded-xl border border-slate-300 bg-white hover:bg-slate-50 text-xs font-semibold text-slate-700 transition-colors"
          >
            Chọn RTSP cho đèn giao thông
          </button>

          {rtspModalOpen ? (
            <div className="fixed inset-0 z-[9998]">
              <button
                type="button"
                className="absolute inset-0 bg-slate-900/30"
                onClick={() => setRtspModalOpen(false)}
                aria-label="Đóng"
              />
              <div className="absolute inset-0 flex items-start justify-center p-4 pt-20">
                <div className="w-[min(36rem,calc(100vw-2rem))] rounded-2xl border border-slate-200 bg-white shadow-2xl overflow-hidden">
                  <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100">
                    <div className="min-w-0">
                      <div className="text-sm font-extrabold text-slate-800 truncate">Chọn RTSP</div>
                      <div className="text-[11px] text-slate-500 truncate">Chỉ hiển thị camera đang chạy ở khu vực chính</div>
                    </div>
                    <button
                      type="button"
                      onClick={() => setRtspModalOpen(false)}
                      className="shrink-0 px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                    >
                      Đóng
                    </button>
                  </div>
                  <RtspAssignModalBody
                    options={filteredCameraOptions}
                    selectedUrls={[selectedUrls[0] ?? '', selectedUrls[1] ?? ''] as const}
                    onSelectUrl={onSelectUrl}
                    activeUrls={activeUrls ?? []}
                  />
                </div>
              </div>
            </div>
          ) : null}
        </>
      ) : null}

      {state ? (
        <div className={!anyRtspSelected ? 'opacity-50 pointer-events-none' : ''}>
          <TrafficLightVisual
            phases={state.phases}
            showAdvice={adviceEnabled}
            phaseTitles={phaseTitles}
            adviceSnapsRef={adviceSnapsRef}
          />
        </div>
      ) : null}
    </div>
  );
}

