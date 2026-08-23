import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useRuns } from '../hooks/queries'
import { formatDateTime, metricsSummary, shortId } from '../lib/format'
import { EmptyState, ErrorState, Loading, Panel } from '../components/common'
import { ModeBadge, StatusBadge } from '../components/StatusBadge'

const MODES = ['backtest', 'shadow', 'live']
const STATUSES = ['running', 'completed', 'halted', 'failed']

const selectCls =
  'rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-accent focus:outline-none'

export default function RunsPage() {
  const [mode, setMode] = useState('')
  const [status, setStatus] = useState('')
  const navigate = useNavigate()
  const runsQ = useRuns({ mode: mode || undefined, status: status || undefined, limit: 200 })
  const runs = runsQ.data ?? []

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-zinc-100">Runs</h1>
        <div className="flex items-center gap-2">
          <select aria-label="Filter by mode" value={mode} onChange={(e) => setMode(e.target.value)} className={selectCls}>
            <option value="">All modes</option>
            {MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <select aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)} className={selectCls}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
      </div>

      <Panel>
        {runsQ.isLoading ? (
          <Loading />
        ) : runsQ.isError ? (
          <ErrorState error={runsQ.error} />
        ) : runs.length === 0 ? (
          <EmptyState text="No runs match the current filters" />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                <th className="pb-2 pr-4 font-medium">ID</th>
                <th className="pb-2 pr-4 font-medium">Mode</th>
                <th className="pb-2 pr-4 font-medium">Strategy</th>
                <th className="pb-2 pr-4 font-medium">Started (UTC)</th>
                <th className="pb-2 pr-4 font-medium">Status</th>
                <th className="pb-2 text-right font-medium">Result</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr
                  key={run.id}
                  onClick={() => void navigate(`/runs/${run.id}`)}
                  className="cursor-pointer border-b border-zinc-800/60 hover:bg-zinc-800/40"
                >
                  <td className="py-2 pr-4 font-mono text-xs">
                    <Link
                      to={`/runs/${run.id}`}
                      onClick={(e) => e.stopPropagation()}
                      className="text-accent hover:underline"
                    >
                      {shortId(run.id)}
                    </Link>
                  </td>
                  <td className="py-2 pr-4">
                    <ModeBadge mode={run.mode} />
                  </td>
                  <td className="py-2 pr-4 text-zinc-300">
                    {run.strategy_id ?? '—'}
                    {run.strategy_version && (
                      <span className="ml-1 text-xs text-zinc-500">v{run.strategy_version}</span>
                    )}
                  </td>
                  <td className="py-2 pr-4 text-zinc-400">{formatDateTime(run.started_at)}</td>
                  <td className="py-2 pr-4">
                    <StatusBadge status={run.status} />
                  </td>
                  <td className="py-2 text-right text-zinc-300">{metricsSummary(run.metrics)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  )
}
