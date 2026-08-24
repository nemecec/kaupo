import { formatNumber } from '../lib/format'

/**
 * Subtle hint shown when a response length equals the requested limit, meaning the
 * backend may have capped it (list endpoints return the latest N rows).
 */
export function CappedNotice({
  length,
  limit,
  noun,
}: {
  length: number
  limit: number
  noun: string
}) {
  if (limit <= 0 || length < limit) return null
  return (
    <span className="text-xs font-normal text-zinc-500">
      showing latest {formatNumber(limit)} {noun} — may be capped
    </span>
  )
}
