import { screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Dashboard from './Dashboard'
import { jsonResponse, renderWithProviders } from '../test/render'
import type { StatusResponse } from '../lib/types'

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
