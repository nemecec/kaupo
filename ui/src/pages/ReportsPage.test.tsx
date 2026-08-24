import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ReportsPage from './ReportsPage'
import { jsonResponse, renderWithProviders } from '../test/render'
import type { DailyReport } from '../lib/types'

const REPORT: DailyReport = {
  period: '2026-08-20',
  generated_at: '2026-08-21T00:05:00Z',
  runs: [],
  totals: { num_runs: 0, active_runs: 0, total_pnl: 0, total_fills: 0, total_fees: 0 },
}

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10)
}

describe('ReportsPage', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.startsWith('/api/v1/reports/daily')) return jsonResponse(REPORT)
        throw new Error(`unexpected fetch: ${url}`)
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('fetches today\'s report initially and a valid typed day on change', async () => {
    const fetchMock = vi.mocked(fetch)
    renderWithProviders(<ReportsPage />)
    await screen.findByText(/no shadow\/live runs/i)

    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes(`day=${todayUtc()}`)),
    ).toBe(true)

    fireEvent.change(screen.getByLabelText('Report day'), { target: { value: '2026-08-01' } })
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes('day=2026-08-01')),
      ).toBe(true)
    })
  })

  it('rejects typed future dates and cleared input', async () => {
    const fetchMock = vi.mocked(fetch)
    renderWithProviders(<ReportsPage />)
    await screen.findByText(/no shadow\/live runs/i)

    const input = screen.getByLabelText('Report day')
    fireEvent.change(input, { target: { value: '2999-01-01' } })
    fireEvent.change(input, { target: { value: '' } })

    // state never changed: input keeps the last valid day, no refetch happened
    expect(input).toHaveValue(todayUtc())
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('2999-01-01'))).toBe(false)
    expect(fetchMock.mock.calls.some(([url]) => /day=$/.test(String(url)))).toBe(false)
  })
})
