import type { DailyReport } from '../lib/types'
import { formatNumber, formatSigned } from '../lib/format'

/** Show denominations separately; older reports cannot establish a currency. */
export function DailyMoney({ totals, field, signed = false }: {
  totals: DailyReport['totals']
  field: 'total_pnl' | 'total_fees'
  signed?: boolean
}) {
  const groups = Object.entries(totals.by_currency ?? {}).sort(([a], [b]) => a.localeCompare(b))
  const unknown = totals.by_currency === undefined || (totals.unclassified_runs ?? 0) > 0
  return (
    <>
      {groups.map(([currency, amounts]) => (
        <div key={currency} className={signed ? (amounts[field] >= 0 ? 'text-emerald-400' : 'text-rose-400') : undefined}>
          {signed ? formatSigned(amounts[field]) : formatNumber(amounts[field])} {currency}
        </div>
      ))}
      {unknown && <div className="text-sm text-zinc-500">Currency unavailable for {totals.unclassified_runs ?? 'legacy'} runs</div>}
      {!unknown && groups.length === 0 && <span>0</span>}
    </>
  )
}
