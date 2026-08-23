/** API types mirroring the FastAPI backend responses. All timestamps are ISO 8601 UTC. */

export interface StatusResponse {
  status: string
  active_runs: number
  runs_by_mode: Record<string, number>
  /** Keyed by "<pair>/<timeframe>", e.g. "BTC/EUR/1h". */
  candles: Record<string, { count: number; latest: string | null }>
}

export type RunMode = 'backtest' | 'shadow' | 'live'
export type RunStatus = 'running' | 'completed' | 'halted' | 'failed'

export interface Run {
  id: string
  mode: RunMode
  strategy_id: string | null
  strategy_version: string | null
  started_at: string
  ended_at: string | null
  status: RunStatus
  config: Record<string, unknown>
  metrics: Record<string, unknown> | null
}

export interface EquityPoint {
  ts: string
  equity: number
  cash: number
  unrealized_pnl: number
}

export interface Order {
  id: string
  ts: string
  pair: string
  side: 'buy' | 'sell'
  type: 'market' | 'limit'
  size: number
  limit_price: number | null
  status: string
  filled_price: number | null
  filled_ts: string | null
  fee: number
  reason: string
}

export interface Trade {
  id: string
  order_id: string
  ts: string
  pair: string
  side: 'buy' | 'sell'
  price: number
  size: number
  fee: number
}

export interface Position {
  pair: string
  size: number
  avg_entry: number
  last_price: number | null
  market_value: number | null
}

export interface Strategy {
  id: string
  version: string
  /** JSON Schema describing the strategy's params. */
  params_schema: Record<string, unknown>
}

export interface BacktestRequest {
  strategy: string
  pair: string
  timeframe?: string
  start?: string
  end?: string
  days?: number
  params?: Record<string, unknown>
  starting_cash?: number
}

/** `run_id` returned by POST /api/v1/backtests is an asynchronous job id. */
export interface BacktestJobHandle {
  run_id: string
}

export interface BacktestJob {
  status: 'running' | 'completed' | 'failed'
  run?: Run
  error?: string
}

export interface DailyReport {
  period: string
  generated_at: string
  /** Per-run entries; keys vary with run activity, render defensively. */
  runs: Record<string, unknown>[]
  totals: {
    num_runs: number
    active_runs: number
    total_pnl: number
    total_fills: number
    total_fees: number
  }
}

export interface Candle {
  ts: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type ControlCommand = 'pause' | 'resume' | 'kill'

export interface ControlResponse {
  command: string
  run_id: string | null
  issued_at: string
}

export interface EventEntry {
  id: string
  ts: string
  level: string
  source: string
  message: string
  data: unknown
}
