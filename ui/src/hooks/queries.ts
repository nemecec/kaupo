import { useEffect, useRef } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { CandlesQuery, RunsFilter } from '../lib/api'
import type { RunStatus } from '../lib/types'

export function useStatus() {
  return useQuery({
    queryKey: ['status'],
    queryFn: api.status,
    refetchInterval: 5_000,
  })
}

export function useRuns(filter: RunsFilter) {
  return useQuery({
    queryKey: ['runs', 'list', filter],
    queryFn: () => api.runs(filter),
    refetchInterval: 15_000,
  })
}

export function useRun(id: string) {
  return useQuery({
    queryKey: ['runs', id],
    queryFn: () => api.run(id),
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 5_000 : false),
  })
}

export function useRunEquity(id: string, live: boolean) {
  return useQuery({
    queryKey: ['runs', id, 'equity'],
    queryFn: () => api.runEquity(id),
    refetchInterval: live ? 10_000 : false,
  })
}

export function useRunOrders(id: string, live: boolean) {
  return useQuery({
    queryKey: ['runs', id, 'orders'],
    queryFn: () => api.runOrders(id),
    refetchInterval: live ? 15_000 : false,
  })
}

export function useRunTrades(id: string, live: boolean) {
  return useQuery({
    queryKey: ['runs', id, 'trades'],
    queryFn: () => api.runTrades(id),
    refetchInterval: live ? 15_000 : false,
  })
}

export function useRunPositions(id: string, live: boolean) {
  return useQuery({
    queryKey: ['runs', id, 'positions'],
    queryFn: () => api.runPositions(id),
    refetchInterval: live ? 15_000 : false,
  })
}

export function useStrategies() {
  return useQuery({
    queryKey: ['strategies'],
    queryFn: api.strategies,
    staleTime: 60_000,
  })
}

export function useDailyReport(day: string) {
  return useQuery({
    queryKey: ['reports', 'daily', day],
    queryFn: () => api.dailyReport(day),
    refetchInterval: 60_000,
  })
}

/**
 * Pass null to disable the query (e.g. run config has no pair yet).
 * keepPreviousData: for live runs the range end moves with each new equity
 * snapshot; keeping the previous page avoids unmounting the chart on every refetch.
 */
export function useCandles(query: CandlesQuery | null) {
  return useQuery({
    queryKey: ['candles', query],
    queryFn: () => api.candles(query as CandlesQuery),
    enabled: query !== null,
    placeholderData: keepPreviousData,
  })
}

export function useEvents(filter: { limit?: number; level?: string }) {
  return useQuery({
    queryKey: ['events', filter],
    queryFn: () => api.events(filter),
    refetchInterval: 10_000,
  })
}


/**
 * One-shot terminal refetch: when a run flips from running to a terminal status,
 * invalidate its queries once so the final equity/orders/fills get fetched —
 * polling stops the moment the status flips and would otherwise miss the tail.
 */
export function useInvalidateRunQueriesOnStop(id: string, status: RunStatus | undefined) {
  const queryClient = useQueryClient()
  const prevStatusRef = useRef<RunStatus | undefined>(undefined)

  useEffect(() => {
    const prev = prevStatusRef.current
    prevStatusRef.current = status
    if (prev === 'running' && status !== undefined && status !== 'running') {
      void queryClient.invalidateQueries({ queryKey: ['runs', id] })
    }
  }, [id, status, queryClient])
}
