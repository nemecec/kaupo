import { Link, useParams } from 'react-router-dom'
import {
  useCandles,
  useRun,
  useRunEquity,
  useRunOrders,
  useRunPositions,
  useRunTrades,
} from '../hooks/queries'
import { formatDateTime, formatNumber, shortId } from '../lib/format'
import { EmptyState, ErrorState, Loading, Panel } from '../components/common'
import { ModeBadge, StatusBadge } from '../components/StatusBadge'
import { MetricCards } from '../components/MetricCards'
import { EquityChart } from '../components/charts/EquityChart'
import { DrawdownChart } from '../components/charts/DrawdownChart'
import { CandleChart } from '../components/charts/CandleChart'
import { equityToLine, tradeMarkers } from '../components/charts/utils'
import type { Order, Trade } from '../lib/types'

function SideCell({ side }: { side: 'buy' | 'sell' }) {
  return (
    <span className={side === 'buy' ? 'text-emerald-400' : 'text-rose-400'}>{side}</span>
  )
}

function OrdersTable({ orders }: { orders: Order[] }) {
  if (orders.length === 0) return <EmptyState text="No orders" />
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
            <th className="pb-2 pr-4 font-medium">Time (UTC)</th>
            <th className="pb-2 pr-4 font-medium">Pair</th>
            <th className="pb-2 pr-4 font-medium">Side</th>
            <th className="pb-2 pr-4 font-medium">Type</th>
            <th className="pb-2 pr-4 text-right font-medium">Size</th>
            <th className="pb-2 pr-4 text-right font-medium">Limit</th>
            <th className="pb-2 pr-4 font-medium">Status</th>
            <th className="pb-2 pr-4 text-right font-medium">Filled</th>
            <th className="pb-2 pr-4 text-right font-medium">Fee</th>
            <th className="pb-2 font-medium">Reason</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id} className="border-b border-zinc-800/60">
              <td className="py-1.5 pr-4 whitespace-nowrap text-zinc-400">{formatDateTime(o.ts)}</td>
              <td className="py-1.5 pr-4 text-zinc-300">{o.pair}</td>
              <td className="py-1.5 pr-4"><SideCell side={o.side} /></td>
              <td className="py-1.5 pr-4 text-zinc-400">{o.type}</td>
              <td className="py-1.5 pr-4 text-right text-zinc-300">{formatNumber(o.size)}</td>
              <td className="py-1.5 pr-4 text-right text-zinc-300">
                {o.limit_price !== null ? formatNumber(o.limit_price) : '—'}
              </td>
              <td className="py-1.5 pr-4 text-zinc-400">{o.status}</td>
              <td className="py-1.5 pr-4 text-right text-zinc-300">
                {o.filled_price !== null ? formatNumber(o.filled_price) : '—'}
              </td>
              <td className="py-1.5 pr-4 text-right text-zinc-400">{formatNumber(o.fee)}</td>
              <td className="py-1.5 max-w-48 truncate text-xs text-zinc-500" title={o.reason}>
                {o.reason}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TradesTable({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) return <EmptyState text="No trades" />
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
            <th className="pb-2 pr-4 font-medium">Time (UTC)</th>
            <th className="pb-2 pr-4 font-medium">Pair</th>
            <th className="pb-2 pr-4 font-medium">Side</th>
            <th className="pb-2 pr-4 text-right font-medium">Price</th>
            <th className="pb-2 pr-4 text-right font-medium">Size</th>
            <th className="pb-2 text-right font-medium">Fee</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.id} className="border-b border-zinc-800/60">
              <td className="py-1.5 pr-4 whitespace-nowrap text-zinc-400">{formatDateTime(t.ts)}</td>
              <td className="py-1.5 pr-4 text-zinc-300">{t.pair}</td>
              <td className="py-1.5 pr-4"><SideCell side={t.side} /></td>
              <td className="py-1.5 pr-4 text-right text-zinc-300">{formatNumber(t.price)}</td>
              <td className="py-1.5 pr-4 text-right text-zinc-300">{formatNumber(t.size)}</td>
              <td className="py-1.5 text-right text-zinc-400">{formatNumber(t.fee)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function RunDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  const runQ = useRun(id)
  const run = runQ.data
  const live = run?.status === 'running'

  const equityQ = useRunEquity(id, live)
  const ordersQ = useRunOrders(id)
  const tradesQ = useRunTrades(id)
  const positionsQ = useRunPositions(id)

  const config = run?.config ?? {}
  const pair = typeof config.pair === 'string' && config.pair !== '' ? config.pair : null
  const timeframe = typeof config.timeframe === 'string' && config.timeframe !== '' ? config.timeframe : '1h'

  // Equity timestamps define the simulation window (backtest equity uses simulated
  // time), so they are a more reliable candle range than run started/ended.
  const equity = equityQ.data ?? []
  const candlesQ = useCandles(
    pair && equity.length > 0
      ? {
          pair,
          timeframe,
          start: equity[0].ts,
          end: equity[equity.length - 1].ts,
          limit: 2000,
        }
      : null,
  )

  const trades = tradesQ.data ?? []
  const candles = candlesQ.data ?? []
  const showCandleChart = pair !== null && candles.length > 0
  const equityMarkerData = !showCandleChart && trades.length > 0 && equity.length > 0
    ? tradeMarkers(trades, equityToLine(equity).map((p) => p.time as number))
    : undefined

  if (runQ.isLoading) return <Loading />
  if (runQ.isError) return <ErrorState error={runQ.error} />
  if (!run) return <EmptyState text="Run not found" />

  return (
    <div className="space-y-6">
      <div>
        <Link to="/runs" className="text-xs text-accent hover:underline">
          ← All runs
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <h1 className="font-mono text-xl font-semibold text-zinc-100">{shortId(run.id)}</h1>
          <ModeBadge mode={run.mode} />
          <StatusBadge status={run.status} />
        </div>
        <p className="mt-1 text-sm text-zinc-500">
          {run.strategy_id ?? 'unknown strategy'}
          {run.strategy_version && <span> v{run.strategy_version}</span>}
          {' · '}started {formatDateTime(run.started_at)}
          {run.ended_at && <> · ended {formatDateTime(run.ended_at)}</>}
        </p>
      </div>

      <MetricCards metrics={run.metrics} />

      <Panel title="Equity">
        {equityQ.isLoading ? (
          <Loading />
        ) : equityQ.isError ? (
          <ErrorState error={equityQ.error} />
        ) : equity.length === 0 ? (
          <EmptyState text="No equity snapshots yet" />
        ) : (
          <EquityChart points={equity} markers={equityMarkerData} />
        )}
      </Panel>

      {equity.length > 0 && (
        <Panel title="Drawdown">
          <DrawdownChart points={equity} />
        </Panel>
      )}

      {showCandleChart && (
        <Panel title={`${pair} · ${timeframe}`}>
          <CandleChart candles={candles} trades={trades} />
        </Panel>
      )}

      {positionsQ.data && positionsQ.data.length > 0 && (
        <Panel title="Open positions">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                <th className="pb-2 pr-4 font-medium">Pair</th>
                <th className="pb-2 pr-4 text-right font-medium">Size</th>
                <th className="pb-2 pr-4 text-right font-medium">Avg entry</th>
                <th className="pb-2 pr-4 text-right font-medium">Last price</th>
                <th className="pb-2 text-right font-medium">Market value</th>
              </tr>
            </thead>
            <tbody>
              {positionsQ.data.map((p) => (
                <tr key={p.pair} className="border-b border-zinc-800/60">
                  <td className="py-1.5 pr-4 text-zinc-300">{p.pair}</td>
                  <td className="py-1.5 pr-4 text-right text-zinc-300">{formatNumber(p.size)}</td>
                  <td className="py-1.5 pr-4 text-right text-zinc-300">{formatNumber(p.avg_entry)}</td>
                  <td className="py-1.5 pr-4 text-right text-zinc-300">
                    {p.last_price !== null ? formatNumber(p.last_price) : '—'}
                  </td>
                  <td className="py-1.5 text-right text-zinc-300">
                    {p.market_value !== null ? formatNumber(p.market_value) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      <Panel title={`Orders${ordersQ.data ? ` (${ordersQ.data.length})` : ''}`}>
        {ordersQ.isLoading ? (
          <Loading />
        ) : ordersQ.isError ? (
          <ErrorState error={ordersQ.error} />
        ) : (
          <OrdersTable orders={ordersQ.data ?? []} />
        )}
      </Panel>

      <Panel title={`Trades${tradesQ.data ? ` (${tradesQ.data.length})` : ''}`}>
        {tradesQ.isLoading ? (
          <Loading />
        ) : tradesQ.isError ? (
          <ErrorState error={tradesQ.error} />
        ) : (
          <TradesTable trades={tradesQ.data ?? []} />
        )}
      </Panel>

      <Panel title="Config">
        <pre className="overflow-x-auto rounded-md bg-zinc-950 p-3 text-xs leading-relaxed text-zinc-300">
          {JSON.stringify(run.config, null, 2)}
        </pre>
      </Panel>
    </div>
  )
}
