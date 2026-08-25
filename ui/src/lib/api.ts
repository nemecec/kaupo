import { getToken } from './auth'
import type {
  BacktestJob,
  BacktestJobHandle,
  BacktestRequest,
  Candle,
  ControlCommand,
  ControlResponse,
  DailyReport,
  EventEntry,
  EquityPoint,
  Order,
  Position,
  Run,
  StatusResponse,
  Strategy,
  Trade,
} from './types'

export class ApiError extends Error {
  /** HTTP status code, or 0 for network-level failures. */
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  const token = getToken()
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  if (init?.body != null && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  let res: Response
  try {
    res = await fetch(path, { ...init, headers })
  } catch (err) {
    throw new ApiError(0, err instanceof Error ? err.message : 'Network error')
  }

  if (!res.ok) {
    let detail = res.statusText
    try {
      const body: unknown = await res.json()
      if (body !== null && typeof body === 'object' && 'detail' in body) {
        const d = (body as { detail: unknown }).detail
        detail = typeof d === 'string' ? d : JSON.stringify(d)
      }
    } catch {
      // non-JSON error body — keep statusText
    }
    throw new ApiError(res.status, `${res.status}: ${detail}`)
  }

  return (await res.json()) as T
}

/** Result of checking a candidate token against the API. */
export type TokenCheck = 'ok' | 'invalid' | 'unreachable'

/** Check a candidate token against the API without persisting it. */
export async function validateToken(token: string): Promise<TokenCheck> {
  let res: Response
  try {
    res = await fetch('/api/v1/status', {
      headers: { Authorization: `Bearer ${token}` },
    })
  } catch {
    return 'unreachable'
  }
  return res.ok ? 'ok' : 'invalid'
}

type QueryValue = string | number | undefined | null

function qs(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      search.set(key, String(value))
    }
  }
  const s = search.toString()
  return s ? `?${s}` : ''
}

export interface RunsFilter {
  mode?: string
  status?: string
  limit?: number
  offset?: number
}

export interface CandlesQuery {
  pair: string
  timeframe?: string
  start?: string
  end?: string
  limit?: number
}

/**
 * Row limits requested from run detail endpoints. The backend returns the LATEST
 * N rows (ascending), so a response whose length equals the limit may be capped —
 * see CappedNotice.
 */
export const EQUITY_LIMIT = 50_000
export const ORDERS_LIMIT = 10_000
export const TRADES_LIMIT = 10_000

export const api = {
  status: () => request<StatusResponse>('/api/v1/status'),

  runs: (filter: RunsFilter = {}) =>
    request<Run[]>(
      `/api/v1/runs${qs({
        mode: filter.mode,
        status: filter.status,
        limit: filter.limit ?? 100,
        offset: filter.offset ?? 0,
      })}`,
    ),

  run: (id: string) => request<Run>(`/api/v1/runs/${id}`),

  runEquity: (id: string) =>
    request<EquityPoint[]>(`/api/v1/runs/${id}/equity${qs({ limit: EQUITY_LIMIT })}`),

  runOrders: (id: string) =>
    request<Order[]>(`/api/v1/runs/${id}/orders${qs({ limit: ORDERS_LIMIT })}`),

  runTrades: (id: string) =>
    request<Trade[]>(`/api/v1/runs/${id}/trades${qs({ limit: TRADES_LIMIT })}`),

  runPositions: (id: string) => request<Position[]>(`/api/v1/runs/${id}/positions`),

  accountEquity: (strategy: string, mode = 'shadow') =>
    request<EquityPoint[]>(`/api/v1/equity/account${qs({ mode, strategy })}`),

  strategies: () => request<Strategy[]>('/api/v1/strategies'),

  startBacktest: (body: BacktestRequest) =>
    request<BacktestJobHandle>('/api/v1/backtests', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  backtestJob: (jobId: string) => request<BacktestJob>(`/api/v1/backtests/${jobId}`),

  dailyReport: (day: string) => request<DailyReport>(`/api/v1/reports/daily${qs({ day })}`),

  candles: (query: CandlesQuery) =>
    request<Candle[]>(
      `/api/v1/candles${qs({
        pair: query.pair,
        timeframe: query.timeframe ?? '1h',
        start: query.start,
        end: query.end,
        limit: query.limit ?? 1000,
      })}`,
    ),

  control: (command: ControlCommand, runId: string | null) =>
    request<ControlResponse>(`/api/v1/control/${command}`, {
      method: 'POST',
      body: JSON.stringify({ run_id: runId }),
    }),

  events: (filter: { limit?: number; level?: string } = {}) =>
    request<EventEntry[]>(
      `/api/v1/events${qs({ limit: filter.limit ?? 200, level: filter.level })}`,
    ),
}
