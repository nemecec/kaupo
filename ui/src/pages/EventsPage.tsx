import { useState } from 'react'
import { useEvents } from '../hooks/queries'
import { formatDateTime, truncate } from '../lib/format'
import { EmptyState, ErrorState, Loading, Panel } from '../components/common'

const LEVELS = ['info', 'warning', 'error', 'critical', 'debug']

const LEVEL_STYLES: Record<string, string> = {
  info: 'border-sky-500/40 bg-sky-500/10 text-sky-400',
  warning: 'border-amber-500/40 bg-amber-500/10 text-amber-400',
  error: 'border-rose-500/40 bg-rose-500/10 text-rose-400',
  critical: 'border-rose-500/60 bg-rose-500/20 text-rose-300',
  debug: 'border-zinc-600 bg-zinc-700/20 text-zinc-400',
}

function LevelBadge({ level }: { level: string }) {
  const cls = LEVEL_STYLES[level] ?? LEVEL_STYLES.debug
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}>
      {level}
    </span>
  )
}

export default function EventsPage() {
  const [level, setLevel] = useState('')
  const eventsQ = useEvents({ level: level || undefined, limit: 200 })
  const events = eventsQ.data ?? []

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-zinc-100">Events</h1>
        <select
          aria-label="Filter by level"
          value={level}
          onChange={(e) => setLevel(e.target.value)}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-accent focus:outline-none"
        >
          <option value="">All levels</option>
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
      </div>

      <Panel>
        {eventsQ.isLoading ? (
          <Loading />
        ) : eventsQ.isError ? (
          <ErrorState error={eventsQ.error} />
        ) : events.length === 0 ? (
          <EmptyState text="No events" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                  <th className="pb-2 pr-4 font-medium">Time (UTC)</th>
                  <th className="pb-2 pr-4 font-medium">Level</th>
                  <th className="pb-2 pr-4 font-medium">Source</th>
                  <th className="pb-2 pr-4 font-medium">Message</th>
                  <th className="pb-2 font-medium">Data</th>
                </tr>
              </thead>
              <tbody>
                {events.map((ev) => (
                  <tr key={ev.id} className="border-b border-zinc-800/60 align-top">
                    <td className="py-1.5 pr-4 whitespace-nowrap text-zinc-400">
                      {formatDateTime(ev.ts)}
                    </td>
                    <td className="py-1.5 pr-4">
                      <LevelBadge level={ev.level} />
                    </td>
                    <td className="py-1.5 pr-4 text-zinc-400">{ev.source}</td>
                    <td className="py-1.5 pr-4 text-zinc-200">{ev.message}</td>
                    <td className="py-1.5 max-w-56">
                      {ev.data == null ? (
                        <span className="text-zinc-700">—</span>
                      ) : (
                        <code className="block truncate text-xs text-zinc-500" title={JSON.stringify(ev.data)}>
                          {truncate(JSON.stringify(ev.data), 120)}
                        </code>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  )
}
