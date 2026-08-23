import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import BacktestPage from './BacktestPage'
import { jsonResponse, renderWithProviders } from '../test/render'
import type { Run, Strategy } from '../lib/types'

const STRATEGIES: Strategy[] = [{ id: 'momentum', version: '1.0.0', params_schema: {} }]

const COMPLETED_RUN: Run = {
  id: 'run-123',
  mode: 'backtest',
  strategy_id: 'momentum',
  strategy_version: '1.0.0',
  started_at: '2026-08-24T10:00:00Z',
  ended_at: '2026-08-24T10:01:00Z',
  status: 'completed',
  config: { pair: 'BTC/EUR', timeframe: '1h' },
  metrics: { total_return_pct: 5.5, num_fills: 8 },
}

function makeFetchMock() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/strategies') return jsonResponse(STRATEGIES)
    if (url === '/api/v1/backtests' && init?.method === 'POST') {
      return jsonResponse({ run_id: 'job-1' }, 202)
    }
    if (url === '/api/v1/backtests/job-1') {
      return jsonResponse({ status: 'completed', run: COMPLETED_RUN })
    }
    throw new Error(`unexpected fetch: ${url}`)
  })
}

describe('BacktestPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', makeFetchMock())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('blocks submission when params are not valid JSON', async () => {
    const fetchMock = vi.mocked(fetch)
    renderWithProviders(<BacktestPage />)

    const paramsField = await screen.findByLabelText(/params/i)
    fireEvent.change(paramsField, { target: { value: '{not json' } })

    expect(await screen.findByText(/invalid json/i)).toBeInTheDocument()
    const submit = screen.getByRole('button', { name: /run backtest/i })
    expect(submit).toBeDisabled()

    fireEvent.click(submit)
    expect(fetchMock.mock.calls.some(([url]) => String(url) === '/api/v1/backtests')).toBe(false)
  })

  it('submits, polls the job and shows the resulting metrics', async () => {
    const fetchMock = vi.mocked(fetch)
    renderWithProviders(<BacktestPage />)

    const submit = await screen.findByRole('button', { name: /run backtest/i })
    await waitFor(() => expect(submit).toBeEnabled())
    fireEvent.click(submit)

    expect(await screen.findByText('Backtest completed')).toBeInTheDocument()
    expect(screen.getByText('5.5%')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /view run/i })).toHaveAttribute('href', '/runs/run-123')

    const postCall = fetchMock.mock.calls.find(([url]) => String(url) === '/api/v1/backtests')
    expect(postCall).toBeDefined()
    const body = JSON.parse(String(postCall?.[1]?.body)) as Record<string, unknown>
    expect(body).toMatchObject({
      strategy: 'momentum',
      pair: 'BTC/EUR',
      timeframe: '1h',
      days: 30,
      starting_cash: 10000,
    })
  })
})
