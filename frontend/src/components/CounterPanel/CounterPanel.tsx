/**
 * CounterPanel – displays vehicle counts.
 * Mode "all":       show total + class breakdown (no direction)
 * Mode "direction": show IN/OUT + class breakdown per direction
 * ROI mode:         show live count (xe trong ROI) + entry stats (xe đã vào ROI)
 */

import type { VehicleStats } from '../../types/detection';

const CLASS_COLORS: Record<string, string> = {
  car:        '#2563eb',
  truck:      '#475569',
  bus:        '#0ea5e9',
  motorcycle: '#64748b',
  motorbike:  '#64748b',
  bicycle:    '#0891b2',
  person:     '#6366f1',
  pedestrian: '#6366f1',
  'container truck': '#334155',
};

interface Props {
  stats: VehicleStats;
  onReset: () => void;
  onExport: () => void;
  /** Camera label shown above the panel (e.g. "Camera 1 — Cổng chính"). */
  cameraLabel?: string;
  compact?: boolean;
  showActions?: boolean;
}

export function CounterPanel({ stats, onReset, onExport, cameraLabel, compact, showActions = true }: Props) {
  const mode = stats.counting_mode ?? 'all';
  const isRoi = stats.roi_active === true;

  // ROI mode
  const roiLive = stats.roi_count ?? 0;
  const roiTotal = stats.roi_total ?? 0;
  const roiEntries = Object.entries(stats.roi_classes ?? {}).sort((a, b) => b[1] - a[1]);
  const roiMaxCount = roiEntries[0]?.[1] || 1;

  // Non-ROI mode: show line-crossing
  const entries = Object.entries(stats.classes).sort((a, b) => b[1] - a[1]);
  const maxCount = entries[0]?.[1] || 1;

  return (
    <div className={`flex flex-col ${compact ? 'gap-2' : 'gap-3'}`}>
      {cameraLabel ? (
        <div className="text-[9px] font-bold uppercase tracking-wider text-slate-400 truncate" title={cameraLabel}>
          {cameraLabel}
        </div>
      ) : null}

      {isRoi ? (
        /* ── ROI mode: live + cumulative ── */
        <>
          <div className="flex items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2">
            <div className="flex items-center gap-2 min-w-0">
              <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse shrink-0" />
              <div className="text-[10px] uppercase tracking-wider text-emerald-700 font-semibold leading-tight">
                Xe trong vùng ROI
              </div>
            </div>
            <span className={`${compact ? 'text-xl' : 'text-2xl'} font-extrabold tabular-nums text-emerald-700 leading-none shrink-0`}>
              {roiLive}
            </span>
          </div>

          <div className="rounded-xl border border-blue-200 bg-blue-50 px-3 py-2.5">
            <div className="text-[10px] uppercase tracking-wider text-blue-500 font-semibold">Xe đã vào ROI</div>
            <div className={`${compact ? 'text-2xl' : 'text-3xl'} font-black text-blue-600 tabular-nums leading-tight`}>{roiTotal}</div>
          </div>

          {roiEntries.length > 0 ? (
            <div className="flex flex-col gap-2">
              {roiEntries.map(([cls, count]) => {
                const c = CLASS_COLORS[cls] || '#94a3b8';
                const pct = Math.max(2, Math.round((count / roiMaxCount) * 100));
                return (
                  <div key={cls} className="rounded-lg border border-slate-200 bg-white px-3 py-2 hover:border-slate-300 transition-colors">
                    <div className="flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: c }} />
                      <span className="flex-1 text-xs font-semibold capitalize text-slate-700 truncate">{cls}</span>
                      <span className="text-xs font-bold tabular-nums text-slate-800 shrink-0">{count}</span>
                    </div>
                    <div className="mt-2 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                      <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, background: c }} />
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
        </>
      ) : (
        /* ── Non-ROI mode: line crossing ── */
        <>
          <div className={`grid gap-2 ${mode === 'direction' ? 'grid-cols-3' : 'grid-cols-1'}`}>
            <div className={`rounded-xl border border-slate-200 bg-white ${compact ? 'px-2.5 py-2' : 'px-3 py-2.5'}`}>
              <div className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Tổng</div>
              <div className={`${compact ? 'text-2xl' : 'text-3xl'} font-black text-accent tabular-nums leading-tight`}>{stats.total}</div>
            </div>
            {mode === 'direction' ? (
              <>
                <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
                  <div className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold flex items-center gap-1.5">
                    <span className="text-green-600">&#x2193;</span> IN
                  </div>
                  <div className="text-2xl font-extrabold text-green-600 tabular-nums leading-tight">{stats.count_in ?? 0}</div>
                </div>
                <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
                  <div className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold flex items-center gap-1.5">
                    <span className="text-orange-500">&#x2191;</span> OUT
                  </div>
                  <div className="text-2xl font-extrabold text-orange-500 tabular-nums leading-tight">{stats.count_out ?? 0}</div>
                </div>
              </>
            ) : null}
          </div>

          {entries.length > 0 ? (
            <div className="flex flex-col gap-2">
              {entries.map(([cls, count]) => {
                const c = CLASS_COLORS[cls] || '#94a3b8';
                const inCount = stats.classes_in?.[cls] ?? 0;
                const outCount = stats.classes_out?.[cls] ?? 0;
                const pct = Math.max(2, Math.round((count / maxCount) * 100));
                return (
                  <div key={cls} className="rounded-lg border border-slate-200 bg-white px-3 py-2 hover:border-slate-300 transition-colors">
                    <div className="flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: c }} />
                      <span className="flex-1 text-xs font-semibold capitalize text-slate-700 truncate">{cls}</span>
                      {mode === 'direction' ? (
                        <span className="text-[10px] tabular-nums shrink-0">
                          <span className="text-green-600 font-semibold" title="IN">{inCount}</span>
                          <span className="text-slate-300"> / </span>
                          <span className="text-orange-500 font-semibold" title="OUT">{outCount}</span>
                        </span>
                      ) : null}
                      <span className="text-xs font-bold tabular-nums text-slate-800 shrink-0">{count}</span>
                    </div>
                    <div className="mt-2 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                      <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, background: c }} />
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
        </>
      )}

      {showActions ? (
        <div className="flex gap-2 pt-1">
          <button
            onClick={onReset}
            className="flex-1 py-2 text-xs font-semibold rounded-lg border border-slate-200 text-slate-700 bg-white hover:bg-slate-50 transition-colors"
          >
            Reset
          </button>
          <button
            onClick={onExport}
            className="flex-1 py-2 text-xs font-bold rounded-lg bg-accent text-white hover:bg-blue-700 transition-colors"
          >
            CSV
          </button>
        </div>
      ) : null}
    </div>
  );
}
