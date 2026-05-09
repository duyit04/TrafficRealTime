import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { trafficLightApi } from '../../services/api';
import type { TLState } from '../../services/api';
import { CameraWall } from '../CameraWall/CameraWall';

const PHASE_LABELS = ['N-S', 'E-W'] as const;

export type TrafficLightPanelProps = {
  phaseRoadLabels?: readonly [string, string];
  /** RTSP URLs currently visible in the main camera area (Camera 1..N). */
  activeUrls?: readonly string[];
  cameraOptions?: readonly { label: string; url: string }[];
  selectedUrls?: readonly [string, string];
  onSelectUrl?: (phaseIndex: 0 | 1, url: string) => void;
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

function TrafficLightVisual({
  phases,
  activePhase,
  getPhaseTitle,
}: {
  phases: TLState['phases'];
  activePhase: number;
  getPhaseTitle: (phaseIndex: number) => string;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      {phases.slice(0, 2).map((p, i) => {
        const isActive = i === activePhase;
        const title = getPhaseTitle(i).trim();
        const color = p.color;

        return (
          <div
            key={i}
            className={`rounded-xl border bg-white px-3 py-3 shadow-sm ${
              isActive ? 'border-accent/40 ring-1 ring-accent/15' : 'border-slate-200'
            }`}
          >
            {title ? (
              <div className="text-[11px] font-bold text-slate-800 leading-snug line-clamp-2" title={title}>
                {title}
              </div>
            ) : null}
            <div className={`text-[9px] text-slate-400 ${title ? 'mt-0.5' : 'mt-0'}`}>
              Camera {i + 1} · {PHASE_LABELS[i]}
            </div>
            <div className="h-2" />

            <div className="mt-3 flex justify-center">
              <div className="bg-slate-900 rounded-2xl p-2.5 shadow-lg border border-slate-800">
                <div className="flex flex-col gap-2.5">
                  {(['red', 'yellow', 'green'] as const).map((c) => {
                    const on = color === c;
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
              </div>
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

export function TrafficLightPanel({ phaseRoadLabels, activeUrls, cameraOptions, selectedUrls, onSelectUrl }: TrafficLightPanelProps = {}) {
  const [state, setState] = useState<TLState | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval>>();

  const selectedTitleFor = useCallback(
    (i: 0 | 1): string => {
      const url = selectedUrls?.[i]?.trim() ?? '';
      if (!url) return '';
      const opt = cameraOptions?.find((c) => c.url === url);
      return (opt?.label ?? url).trim();
    },
    [cameraOptions, selectedUrls],
  );

  const getPhaseTitle = useCallback(
    (i: number) => {
      const idx = (i & 1) as 0 | 1;
      const t = selectedTitleFor(idx);
      if (t) return t;
      return phaseRoadLabels?.[idx]?.trim() ?? '';
    },
    [phaseRoadLabels, selectedTitleFor],
  );

  // Poll state
  useEffect(() => {
    const poll = async () => {
      try {
        const s = await trafficLightApi.getState();
        setState(s);
      } catch {
        // ignore
      }
    };
    poll();
    pollRef.current = setInterval(poll, 1000);
    return () => clearInterval(pollRef.current);
  }, []);

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
          <TrafficLightVisual phases={state.phases} activePhase={state.active_phase} getPhaseTitle={getPhaseTitle} />
        </div>
      ) : null}
    </div>
  );
}

