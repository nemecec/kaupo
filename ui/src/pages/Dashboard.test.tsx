import { screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Dashboard from './Dashboard'
import { jsonResponse, renderWithProviders } from '../test/render'
import type { EquityPoint, Run, StatusResponse } from '../lib/types'

// jsdom has no canvas: fake the chart so the account-equity panel can render
vi.mock('lightweight-charts', () => ({
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
  LineSeries: 'LineSeries',
  AreaSeries: 'AreaSeries',
  createSeriesMarkers: vi.fn(),
  createChart: () => ({
    addSeries: () => ({ setData: vi.fn() }),
    removeSeries: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: vi.fn(),
  }),
}))

const STATUS: StatusResponse = {
  status: 'ok',
  active_runs: 0,
  runs_by_mode: {},
  candles: {},
}

describe('Dashboard', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url === '/api/v1/status') return jsonResponse(STATUS)
        if (url.startsWith('/api/v1/reports/daily')) return jsonResponse({ detail: 'boom' }, 500)
        if (url.startsWith('/api/v1/runs')) return jsonResponse([])
        throw new Error(`unexpected fetch: ${url}`)
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows "report unavailable" when the daily report fails, not "no report yet"', async () => {
    renderWithProviders(<Dashboard />)
    expect(await screen.findByText(/report unavailable/i)).toBeInTheDocument()
    // other cards still render their data
    expect(screen.getByText('Active runs')).toBeInTheDocument()
    expect(screen.getByText('No runs currently running')).toBeInTheDocument()
  })
})

const SHADOW_RUN: Run = {
  id: 'run-1',
  mode: 'shadow',
  strategy_id: 'regime-switch',
  strategy_version: 'v1',
  started_at: '2026-01-01T00:00:00Z',
  ended_at: null,
  status: 'running',
  config: {},
  metrics: null,
}

const ACCOUNT_POINTS: EquityPoint[] = [
  { ts: '2026-01-01T00:00:00Z', equity: 10_000, cash: 10_000, unrealized_pnl: 0 },
  { ts: '2026-01-02T00:00:00Z', equity: 10_150, cash: 10_150, unrealized_pnl: 0 },
]

describe('Dashboard account equity panel', () => {
  function mockFetch({ shadowRuns, accountPoints }: { shadowRuns: Run[]; accountPoints: EquityPoint[] }) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url === '/api/v1/status') return jsonResponse(STATUS)
        if (url.startsWith('/api/v1/reports/daily')) return jsonResponse({ detail: 'boom' }, 500)
        if (url.startsWith('/api/v1/equity/account')) return jsonResponse(accountPoints)
        if (url.startsWith('/api/v1/runs')) {
          return jsonResponse(url.includes('mode=shadow') ? shadowRuns : [])
        }
        throw new Error(`unexpected fetch: ${url}`)
      }),
    )
  }

  beforeEach(() => {
    mockFetch({ shadowRuns: [SHADOW_RUN], accountPoints: ACCOUNT_POINTS })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the stitched series for the newest shadow run', async () => {
    renderWithProviders(<Dashboard />)
    expect(await screen.findByText('Account equity')).toBeInTheDocument()
    expect(await screen.findByTestId('equity-chart')).toBeInTheDocument()
    // queried the account endpoint for the run's strategy in shadow mode
    const calls = vi.mocked(fetch).mock.calls.map((c) => String(c[0]))
    expect(calls).toContain('/api/v1/equity/account?mode=shadow&strategy=regime-switch')
  })

  it('is hidden when no shadow run exists', async () => {
    mockFetch({ shadowRuns: [], accountPoints: ACCOUNT_POINTS })
    renderWithProviders(<Dashboard />)
    expect(await screen.findByText('No runs currently running')).toBeInTheDocument()
    expect(screen.queryByText('Account equity')).not.toBeInTheDocument()
    const calls = vi.mocked(fetch).mock.calls.map((c) => String(c[0]))
    expect(calls.some((u) => u.startsWith('/api/v1/equity/account'))).toBe(false)
  })

  it('is hidden when the stitched series is empty', async () => {
    mockFetch({ shadowRuns: [SHADOW_RUN], accountPoints: [] })
    renderWithProviders(<Dashboard />)
    expect(await screen.findByText('No runs currently running')).toBeInTheDocument()
    // the panel may show a loading state first, then must disappear entirely
    await waitFor(() => {
      expect(screen.queryByText('Account equity')).not.toBeInTheDocument()
    })
  })
})
