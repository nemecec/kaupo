import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { CandlesQuery, RunsFilter } from '../lib/api'

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

export function useRunOrders(id: string) {
  return useQuery({
    queryKey: ['runs', id, 'orders'],
    queryFn: () => api.runOrders(id),
  })
}

export function useRunTrades(id: string) {
  return useQuery({
    queryKey: ['runs', id, 'trades'],
    queryFn: () => api.runTrades(id),
  })
}

export function useRunPositions(id: string) {
  return useQuery({
    queryKey: ['runs', id, 'positions'],
    queryFn: () => api.runPositions(id),
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
  })
}

/** Pass null to disable the query (e.g. run config has no pair yet). */
export function useCandles(query: CandlesQuery | null) {
  return useQuery({
    queryKey: ['candles', query],
    queryFn: () => api.candles(query as CandlesQuery),
    enabled: query !== null,
  })
}

export function useEvents(filter: { limit?: number; level?: string }) {
  return useQuery({
    queryKey: ['events', filter],
    queryFn: () => api.events(filter),
    refetchInterval: 10_000,
  })
}
