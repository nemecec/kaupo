import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { useAccountEquity, useInvalidateRunQueriesOnStop } from './queries'
import { api } from '../lib/api'
import type { RunStatus } from '../lib/types'

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

it('keeps account equity caches separate across currencies', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const spy = vi.spyOn(api, 'accountEquity').mockImplementation(async (_strategy, _mode, currency) => [
    { ts: '2026-01-01T00:00:00Z', equity: currency === 'EUR' ? 100 : 900, cash: 0, unrealized_pnl: 0 },
  ])
  try {
    const { result, rerender, unmount } = renderHook(
      ({ currency }) => useAccountEquity('s', 'shadow', currency),
      { wrapper: makeWrapper(client), initialProps: { currency: 'EUR' } },
    )
    await waitFor(() => expect(result.current.data?.[0].equity).toBe(100))
    rerender({ currency: 'USD' })
    await waitFor(() => expect(result.current.data?.[0].equity).toBe(900))
    rerender({ currency: 'EUR' })
    await waitFor(() => expect(result.current.data?.[0].equity).toBe(100))
    expect(spy).toHaveBeenCalledTimes(2)
    unmount()
  } finally {
    spy.mockRestore()
    client.clear()
  }
})

describe('useInvalidateRunQueriesOnStop', () => {
  it('invalidates the run queries exactly once on the running -> terminal transition', () => {
    const client = new QueryClient()
    const spy = vi.spyOn(client, 'invalidateQueries')
    const { rerender } = renderHook(
      (props: { status: RunStatus | undefined }) => useInvalidateRunQueriesOnStop('run-1', props.status),
      {
        wrapper: makeWrapper(client),
        initialProps: { status: 'running' as RunStatus | undefined },
      },
    )

    rerender({ status: 'running' })
    expect(spy).not.toHaveBeenCalled()

    rerender({ status: 'completed' })
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith({ queryKey: ['runs', 'run-1'] })

    // staying terminal does not invalidate again
    rerender({ status: 'completed' })
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('does not invalidate when the run was never observed running', () => {
    const client = new QueryClient()
    const spy = vi.spyOn(client, 'invalidateQueries')
    renderHook(() => useInvalidateRunQueriesOnStop('run-1', 'completed'), {
      wrapper: makeWrapper(client),
    })
    expect(spy).not.toHaveBeenCalled()
  })
})
