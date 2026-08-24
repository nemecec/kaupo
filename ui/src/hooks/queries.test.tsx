import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { useInvalidateRunQueriesOnStop } from './queries'
import type { RunStatus } from '../lib/types'

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

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
