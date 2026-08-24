/** Display formatting helpers. */

export function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d
    .toLocaleString('en-GB', {
      year: 'numeric',
      month: 'short',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'UTC',
    })
    .replace(',', '')
}

export function formatNumber(v: number): string {
  if (!Number.isFinite(v)) return String(v)
  if (v === 0) return '0' // also normalizes -0
  const abs = Math.abs(v)
  // dust values would round to "0" with fixed fraction digits — use significant digits
  if (abs < 1e-6) return v.toPrecision(2)
  const maxDigits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: maxDigits }).format(v)
}

export function formatSigned(v: number): string {
  return `${v > 0 ? '+' : ''}${formatNumber(v)}`
}

/** Percentage values (already in percent units) with at most 2 decimals. */
export function formatPercent(v: number): string {
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(v)}%`
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/** Human label for a snake_case metric key: "total_return_pct" -> "total return pct". */
export function metricLabel(key: string): string {
  return key.replace(/_/g, ' ')
}

/** Renders an arbitrary metrics value. Keys ending in `_pct` / containing `rate` get a % suffix. */
export function metricValueToString(key: string, value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    return key.endsWith('_pct') || key.includes('rate') ? formatPercent(value) : formatNumber(value)
  }
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

/** One-line metrics summary for table rows. */
export function metricsSummary(metrics: Record<string, unknown> | null): string {
  if (!metrics) return '—'
  const ret = metrics.total_return_pct
  if (typeof ret === 'number') return `${ret > 0 ? '+' : ''}${formatPercent(ret)}`
  const pnl = metrics.total_pnl
  if (typeof pnl === 'number') return formatSigned(pnl)
  const fills = metrics.num_fills
  if (typeof fills === 'number') return `${fills} fills`
  const keys = Object.keys(metrics)
  return keys.length > 0 ? `${keys.length} metrics` : '—'
}

export function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max)}…` : s
}
