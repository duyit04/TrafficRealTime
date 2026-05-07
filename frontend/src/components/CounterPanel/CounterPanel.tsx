/**
 * CounterPanel – displays vehicle counts.
 * Mode "all":       show total + class breakdown (no direction)
 * Mode "direction": show IN/OUT + class breakdown per direction
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
}

export function CounterPanel({ stats, onReset, onExport }: Props) {
  const mode = stats.counting_mode ?? 'all';
  const entries = Object.entries(stats.classes).sort((a, b) => b[1] - a[1]);
  const maxCount = entries[0]?.[1] || 1;

  return (
    <div className="flex flex-col gap-3">

      {/* ── Summary ── */}
      <div className={`grid gap-2 ${mode === 'direction' ? 'grid-cols-3' : 'grid-cols-1'}`}>
        <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
          <div className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Tổng</div>
          <div className="text-3xl font-black text-accent tabular-nums leading-tight">{stats.total}</div>
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

      {/* ── Per-class breakdown ── */}
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

      {/* Action buttons */}
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
    </div>
  );
}
