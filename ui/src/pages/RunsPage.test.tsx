import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import RunsPage from './RunsPage'
import { jsonResponse, renderWithProviders } from '../test/render'
import type { Run } from '../lib/types'

const RUNS: Run[] = [
  {
    id: 'a1b2c3d4-1111-4000-8000-000000000000',
    mode: 'live',
    strategy_id: 'momentum',
    strategy_version: '1.2.0',
    started_at: '2026-08-20T10:00:00Z',
    ended_at: null,
    status: 'running',
    config: { pair: 'BTC/EUR' },
    metrics: null,
  },
  {
    id: 'e5f60718-2222-4000-8000-000000000000',
    mode: 'backtest',
    strategy_id: 'meanrev',
    strategy_version: '0.9.0',
    started_at: '2026-08-19T09:30:00Z',
    ended_at: '2026-08-19T09:31:00Z',
    status: 'completed',
    config: { pair: 'ETH/EUR' },
    metrics: { total_return_pct: 3.21, num_fills: 17 },
  },
]

describe('RunsPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(RUNS)))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the runs table with strategy and result summary', async () => {
    renderWithProviders(<RunsPage />)
    expect(await screen.findByText('momentum')).toBeInTheDocument()
    expect(screen.getByText('meanrev')).toBeInTheDocument()
    expect(screen.getByText('a1b2c3d4')).toBeInTheDocument()
    expect(screen.getByText('+3.21%')).toBeInTheDocument()
    expect(screen.getByText('v1.2.0')).toBeInTheDocument()
  })

  it('refetches with the mode filter applied', async () => {
    const fetchMock = vi.mocked(fetch)
    renderWithProviders(<RunsPage />)
    await screen.findByText('momentum')

    fireEvent.change(screen.getByLabelText('Filter by mode'), { target: { value: 'live' } })

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes('mode=live')),
      ).toBe(true)
    })
  })

  it('shows a cap hint when the list fills the page limit', async () => {
    const many: Run[] = Array.from({ length: 200 }, (_, i) => ({
      ...RUNS[0],
      id: `c${String(i).padStart(7, '0')}-fill`,
    }))
    vi.mocked(fetch).mockImplementation(async () => jsonResponse(many))
    renderWithProviders(<RunsPage />)
    expect(await screen.findByText(/showing latest 200 runs/i)).toBeInTheDocument()
  })
})
