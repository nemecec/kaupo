import { Link } from 'react-router-dom'
import { useQueries } from '@tanstack/react-query'
import { api } from '../lib/api'
import { formatDateTime, formatNumber, formatSigned, shortId } from '../lib/format'
import { useDailyReport, useRuns, useStatus } from '../hooks/queries'
import { EmptyState, ErrorState, Loading, Panel } from '../components/common'
import { KillSwitch } from '../components/KillSwitch'
import { MultiEquityChart, type NamedSeries } from '../components/charts/MultiEquityChart'
import { equityToLine } from '../components/charts/utils'

const SERIES_PALETTE = ['#0072ce', '#10b981', '#f59e0b', '#a855f7', '#f43f5e', '#22d3ee']

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10)
}

function StatCard({ label, children, sub }: { label: string; children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</div>
      <div className="mt-1.5 text-2xl font-semibold text-zinc-100">{children}</div>
      {sub && <div className="mt-1 text-xs text-zinc-500">{sub}</div>}
    </div>
  )
}

export default function Dashboard() {
  const statusQ = useStatus()
  const reportQ = useDailyReport(todayUtc())
  const runningQ = useRuns({ status: 'running', limit: 50 })
  const runningRuns = runningQ.data ?? []

  const equityQueries = useQueries({
    queries: runningRuns.map((run) => ({
      queryKey: ['runs', run.id, 'equity'],
      queryFn: () => api.runEquity(run.id),
      refetchInterval: 10_000,
    })),
  })

  const seriesList: NamedSeries[] = runningRuns.flatMap((run, i) => {
    const data = equityQueries[i]?.data
    if (!data || data.length === 0) return []
    return [
      {
        name: shortId(run.id),
        color: SERIES_PALETTE[i % SERIES_PALETTE.length],
        points: equityToLine(data),
      },
    ]
  })

  const status = statusQ.data
  const totals = reportQ.data?.totals
  const candleEntries = Object.entries(status?.candles ?? {})
  const latestCandle = candleEntries.reduce<string | null>((acc, [, v]) => {
    if (!v.latest) return acc
    return acc === null || v.latest > acc ? v.latest : acc
  }, null)

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-zinc-100">Dashboard</h1>
        <KillSwitch />
      </div>

      {statusQ.isError && <ErrorState error={statusQ.error} />}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          label="Active runs"
          sub={
            status
              ? Object.entries(status.runs_by_mode)
                  .map(([mode, n]) => `${mode} ${n}`)
                  .join(' · ') || 'none'
              : undefined
          }
        >
          {status ? status.active_runs : '—'}
        </StatCard>

        <StatCard
          label="Today P&L"
          sub={
            totals
              ? `${totals.total_fills} fills · fees ${formatNumber(totals.total_fees)}`
              : reportQ.isError
                ? 'no report yet'
                : undefined
          }
        >
          {totals ? (
            <span className={totals.total_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
              {formatSigned(totals.total_pnl)}
            </span>
          ) : (
            '—'
          )}
        </StatCard>

        <StatCard
          label="Today runs"
          sub={totals ? `${totals.active_runs} active` : undefined}
        >
          {totals ? totals.num_runs : '—'}
        </StatCard>

        <StatCard
          label="Market data"
          sub={latestCandle ? `latest ${formatDateTime(latestCandle)}` : 'no candles'}
        >
          {status ? candleEntries.length : '—'}
          <span className="ml-1 text-sm font-normal text-zinc-500">feeds</span>
        </StatCard>
      </div>

      <Panel
        title="Running equity"
        action={
          <Link to="/reports" className="text-xs text-accent hover:underline">
            Daily report →
          </Link>
        }
      >
        {runningQ.isLoading ? (
          <Loading />
        ) : runningQ.isError ? (
          <ErrorState error={runningQ.error} />
        ) : runningRuns.length === 0 ? (
          <EmptyState text="No runs currently running" />
        ) : seriesList.length === 0 ? (
          <Loading text="Waiting for equity snapshots…" />
        ) : (
          <MultiEquityChart seriesList={seriesList} />
        )}
      </Panel>

      {candleEntries.length > 0 && (
        <Panel title="Candle feeds">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-zinc-500">
                <th className="pb-2 font-medium">Feed</th>
                <th className="pb-2 text-right font-medium">Candles</th>
                <th className="pb-2 text-right font-medium">Latest</th>
              </tr>
            </thead>
            <tbody>
              {candleEntries.map(([key, v]) => (
                <tr key={key} className="border-t border-zinc-800/60">
                  <td className="py-1.5 font-mono text-xs text-zinc-300">{key}</td>
                  <td className="py-1.5 text-right text-zinc-300">{formatNumber(v.count)}</td>
                  <td className="py-1.5 text-right text-zinc-400">{formatDateTime(v.latest)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  )
}
