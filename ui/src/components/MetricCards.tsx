import { metricLabel, metricValueToString } from '../lib/format'
import { EmptyState } from './common'

function valueClass(key: string, value: unknown): string {
  if (typeof value === 'number' && (key.includes('return') || key.includes('pnl'))) {
    if (value > 0) return 'text-emerald-400'
    if (value < 0) return 'text-rose-400'
  }
  return 'text-zinc-100'
}

/** Renders an arbitrary metrics object as a grid of key/value cards. */
export function MetricCards({ metrics }: { metrics: Record<string, unknown> | null | undefined }) {
  if (!metrics || Object.keys(metrics).length === 0) {
    return <EmptyState text="No metrics recorded" />
  }
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
      {Object.entries(metrics).map(([key, value]) => (
        <div key={key} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
          <dt className="truncate text-xs text-zinc-500" title={metricLabel(key)}>
            {metricLabel(key)}
          </dt>
          <dd className={`mt-1 truncate text-lg font-semibold ${valueClass(key, value)}`} title={metricValueToString(key, value)}>
            {metricValueToString(key, value)}
          </dd>
        </div>
      ))}
    </dl>
  )
}
