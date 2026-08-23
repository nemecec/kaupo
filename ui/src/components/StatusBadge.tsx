const STATUS_STYLES: Record<string, string> = {
  running: 'border-sky-500/40 bg-sky-500/10 text-sky-400',
  completed: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400',
  halted: 'border-amber-500/40 bg-amber-500/10 text-amber-400',
  failed: 'border-rose-500/40 bg-rose-500/10 text-rose-400',
}

const MODE_STYLES: Record<string, string> = {
  live: 'border-accent/50 bg-accent/10 text-accent',
  shadow: 'border-violet-500/40 bg-violet-500/10 text-violet-400',
  backtest: 'border-zinc-600 bg-zinc-700/20 text-zinc-400',
}

function Badge({ value, styles }: { value: string; styles: Record<string, string> }) {
  const cls = styles[value] ?? 'border-zinc-600 bg-zinc-700/20 text-zinc-400'
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}>
      {value}
    </span>
  )
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge value={status} styles={STATUS_STYLES} />
}

export function ModeBadge({ mode }: { mode: string }) {
  return <Badge value={mode} styles={MODE_STYLES} />
}
