import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { BacktestRequest } from '../lib/types'
import { useStrategies } from '../hooks/queries'
import { EmptyState, ErrorState, Loading, Panel } from '../components/common'
import { MetricCards } from '../components/MetricCards'
import { ModeBadge, StatusBadge } from '../components/StatusBadge'

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']

const inputCls =
  'w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 placeholder-zinc-600 focus:border-accent focus:outline-none'

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <div>
      <label className="mb-1 block text-xs font-medium text-zinc-400">{label}</label>
      {children}
      {hint && <p className="mt-1 text-xs text-zinc-600">{hint}</p>}
    </div>
  )
}

/** Parses the params textarea; returns [params, errorMessage]. */
function parseParams(text: string): [Record<string, unknown> | undefined, string | null] {
  const trimmed = text.trim()
  if (trimmed === '') return [undefined, null]
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return [undefined, 'Params must be a JSON object']
    }
    return [parsed as Record<string, unknown>, null]
  } catch (err) {
    return [undefined, `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`]
  }
}

export default function BacktestPage() {
  const strategiesQ = useStrategies()
  const strategies = strategiesQ.data ?? []

  const [strategyChoice, setStrategyChoice] = useState('')
  const [pair, setPair] = useState('BTC/EUR')
  const [timeframe, setTimeframe] = useState('1h')
  const [days, setDays] = useState('30')
  const [startingCash, setStartingCash] = useState('10000')
  const [paramsText, setParamsText] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)

  const selectedStrategy = strategyChoice || strategies[0]?.id || ''
  const [params, paramsError] = parseParams(paramsText)
  const daysNum = Number(days)
  const cashNum = Number(startingCash)

  const startMutation = useMutation({
    mutationFn: (body: BacktestRequest) => api.startBacktest(body),
    onSuccess: (data) => setJobId(data.run_id),
  })

  const jobQ = useQuery({
    queryKey: ['backtests', 'job', jobId],
    queryFn: () => api.backtestJob(jobId as string),
    enabled: jobId !== null,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2_000 : false),
  })

  const job = jobQ.data
  const canSubmit =
    selectedStrategy !== '' &&
    pair.trim() !== '' &&
    paramsError === null &&
    Number.isFinite(daysNum) &&
    daysNum >= 1 &&
    Number.isFinite(cashNum) &&
    cashNum > 0 &&
    !startMutation.isPending

  const reset = () => {
    setJobId(null)
    startMutation.reset()
  }

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!canSubmit) return
    const body: BacktestRequest = {
      strategy: selectedStrategy,
      pair: pair.trim(),
      timeframe,
      days: daysNum,
      starting_cash: cashNum,
      ...(params ? { params } : {}),
    }
    startMutation.mutate(body)
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-zinc-100">Backtest</h1>

      <Panel title="New backtest">
        {strategiesQ.isLoading ? (
          <Loading />
        ) : strategiesQ.isError ? (
          <ErrorState error={strategiesQ.error} />
        ) : strategies.length === 0 ? (
          <EmptyState text="No strategies registered on the server" />
        ) : (
          <form onSubmit={onSubmit} className="grid max-w-3xl grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label="Strategy">
              <select
                aria-label="Strategy"
                value={selectedStrategy}
                onChange={(e) => setStrategyChoice(e.target.value)}
                className={inputCls}
              >
                {strategies.map((s) => (
                  <option key={`${s.id}@${s.version}`} value={s.id}>
                    {s.id} (v{s.version})
                  </option>
                ))}
              </select>
            </Field>

            <Field label="Pair">
              <input
                aria-label="Pair"
                value={pair}
                onChange={(e) => setPair(e.target.value)}
                placeholder="BTC/EUR"
                className={inputCls}
                required
              />
            </Field>

            <Field label="Timeframe">
              <select aria-label="Timeframe" value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className={inputCls}>
                {TIMEFRAMES.map((tf) => (
                  <option key={tf} value={tf}>
                    {tf}
                  </option>
                ))}
              </select>
            </Field>

            <Field label="Days">
              <input
                aria-label="Days"
                type="number"
                min={1}
                value={days}
                onChange={(e) => setDays(e.target.value)}
                className={inputCls}
                required
              />
            </Field>

            <Field label="Starting cash">
              <input
                aria-label="Starting cash"
                type="number"
                min={1}
                step="any"
                value={startingCash}
                onChange={(e) => setStartingCash(e.target.value)}
                className={inputCls}
                required
              />
            </Field>

            <div className="sm:col-span-2">
              <Field label="Params (JSON, optional)">
                <textarea
                  aria-label="Params (JSON, optional)"
                  value={paramsText}
                  onChange={(e) => setParamsText(e.target.value)}
                  placeholder='{"fast": 12, "slow": 26}'
                  rows={4}
                  spellCheck={false}
                  className={`${inputCls} font-mono text-xs`}
                />
              </Field>
              {paramsError && <p className="mt-1 text-xs text-rose-400">{paramsError}</p>}
            </div>

            <div className="flex items-center gap-3 sm:col-span-2">
              <button
                type="submit"
                disabled={!canSubmit}
                className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {startMutation.isPending ? 'Starting…' : 'Run backtest'}
              </button>
              {startMutation.isError && <ErrorState error={startMutation.error} />}
            </div>
          </form>
        )}
      </Panel>

      {jobId && (
        <Panel
          title={
            <span>
              Backtest job <span className="font-mono text-xs text-zinc-500">{jobId}</span>
            </span>
          }
          action={
            <button type="button" onClick={reset} className="text-xs text-zinc-400 hover:text-zinc-200">
              Dismiss
            </button>
          }
        >
          {jobQ.isLoading || job?.status === 'running' ? (
            <Loading text="Backtest running…" />
          ) : jobQ.isError ? (
            <ErrorState error={jobQ.error} />
          ) : job?.status === 'failed' ? (
            <div role="alert" className="rounded-md border border-rose-900/60 bg-rose-950/40 px-3 py-2 text-sm text-rose-300">
              Backtest failed{job.error ? `: ${job.error}` : ''}
            </div>
          ) : job?.status === 'completed' && job.run ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-3">
                <span className="text-sm font-medium text-emerald-400">Backtest completed</span>
                <ModeBadge mode={job.run.mode} />
                <StatusBadge status={job.run.status} />
                <Link to={`/runs/${job.run.id}`} className="text-sm text-accent hover:underline">
                  View run →
                </Link>
              </div>
              <MetricCards metrics={job.run.metrics} />
            </div>
          ) : (
            <Loading text="Backtest running…" />
          )}
        </Panel>
      )}
    </div>
  )
}
