import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useDailyReport } from '../hooks/queries'
import { formatDateTime, formatNumber, formatSigned, isRecord, metricLabel, shortId } from '../lib/format'
import { EmptyState, ErrorState, Loading, Panel } from '../components/common'
import { StatusBadge } from '../components/StatusBadge'
import { DailyMoney } from '../components/DailyMoney'

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10)
}

function TotalCard({ label, value, colored }: { label: string; value: number; colored?: boolean }) {
  const cls = colored ? (value >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-zinc-100'
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`mt-1.5 text-xl font-semibold ${cls}`}>
        {colored ? formatSigned(value) : formatNumber(value)}
      </div>
    </div>
  )
}

/** Preferred column order for per-run report entries; unknown keys are appended. */
const PREFERRED_COLUMNS = [
  'run_id',
  'mode',
  'strategy_id',
  'status',
  'activity_state',
  'activity_reason',
  'last_snapshot_at',
  'quote_currency',
  'pnl',
  'num_fills',
  'fees_paid',
  'round_trips',
  'winning_trips',
  'start_equity',
  'end_equity',
]

function reportColumns(runs: Record<string, unknown>[]): string[] {
  const keys = new Set<string>()
  for (const run of runs) {
    for (const k of Object.keys(run)) keys.add(k)
  }
  const preferred = PREFERRED_COLUMNS.filter((k) => keys.has(k))
  const rest = [...keys].filter((k) => !PREFERRED_COLUMNS.includes(k)).sort()
  return [...preferred, ...rest]
}

function ActivityBadge({ state }: { state: string }) {
  const styles: Record<string, string> = {
    active: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400',
    market_closed: 'border-amber-500/40 bg-amber-500/10 text-amber-400',
    inactive: 'border-zinc-600 bg-zinc-700/20 text-zinc-400',
  }
  const labels: Record<string, string> = { active: 'Active', market_closed: 'Market closed', inactive: 'Inactive' }
  return <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${styles[state] ?? styles.inactive}`}>{labels[state] ?? state}</span>
}

function ReportCell({ column, value }: { column: string; value: unknown }) {
  if (value === null || value === undefined) return <span className="text-zinc-600">—</span>
  if (column === 'run_id' && typeof value === 'string') {
    return (
      <Link to={`/runs/${value}`} className="font-mono text-xs text-accent hover:underline">
        {shortId(value)}
      </Link>
    )
  }
  if (column === 'status' && typeof value === 'string') return <StatusBadge status={value} />
  if (column === 'activity_state' && typeof value === 'string') return <ActivityBadge state={value} />
  if (column === 'last_snapshot_at' && typeof value === 'string') return <span className="text-zinc-300">{formatDateTime(value)}</span>
  if (typeof value === 'number') {
    if (column === 'pnl') {
      return (
        <span className={value >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{formatSigned(value)}</span>
      )
    }
    return <span className="text-zinc-300">{formatNumber(value)}</span>
  }
  if (typeof value === 'boolean') {
    return <span className="text-zinc-400">{value ? 'yes' : 'no'}</span>
  }
  if (typeof value === 'string') return <span className="text-zinc-300">{value}</span>
  if (isRecord(value) || Array.isArray(value)) {
    return <span className="font-mono text-xs text-zinc-500">{JSON.stringify(value)}</span>
  }
  return <span className="text-zinc-300">{String(value)}</span>
}

export default function ReportsPage() {
  const [day, setDay] = useState(todayUtc)
  const reportQ = useDailyReport(day)
  const report = reportQ.data

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-zinc-100">Daily report</h1>
        <input
          aria-label="Report day"
          type="date"
          value={day}
          max={todayUtc()}
          onChange={(e) => {
            // ignore a cleared input (keep the last valid day) and reject typed
            // future dates — the max attribute only constrains the picker (Safari)
            if (e.target.value && e.target.value <= todayUtc()) setDay(e.target.value)
          }}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-accent focus:outline-none"
        />
      </div>

      {reportQ.isLoading ? (
        <Loading />
      ) : reportQ.isError ? (
        <ErrorState error={reportQ.error} />
      ) : !report ? (
        <EmptyState text="No report" />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
            <TotalCard label="Runs" value={report.totals.num_runs} />
            <TotalCard label="Active" value={report.totals.active_runs} />
            {report.totals.market_closed_runs !== undefined && <TotalCard label="Market closed" value={report.totals.market_closed_runs} />}
            {report.totals.inactive_runs !== undefined && <TotalCard label="Inactive" value={report.totals.inactive_runs} />}
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
              <div className="text-xs text-zinc-500">P&L by currency</div>
              <DailyMoney totals={report.totals} field="total_pnl" signed />
            </div>
            <TotalCard label="Fills" value={report.totals.total_fills} />
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
              <div className="text-xs text-zinc-500">Fees by currency</div>
              <DailyMoney totals={report.totals} field="total_fees" />
            </div>
          </div>

          <Panel
            title={`Runs on ${report.period}`}
            action={
              <span className="text-xs text-zinc-500">generated {formatDateTime(report.generated_at)}</span>
            }
          >
            {report.runs.length === 0 ? (
              <EmptyState text="No shadow/live runs were active on this day" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                      {reportColumns(report.runs).map((col) => (
                        <th key={col} className="pb-2 pr-4 font-medium whitespace-nowrap">
                          {metricLabel(col)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {report.runs.map((run, i) => (
                      <tr key={typeof run.run_id === 'string' ? run.run_id : i} className="border-b border-zinc-800/60">
                        {reportColumns(report.runs).map((col) => (
                          <td key={col} className="py-1.5 pr-4 whitespace-nowrap">
                            <ReportCell column={col} value={run[col]} />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </>
      )}
    </div>
  )
}
