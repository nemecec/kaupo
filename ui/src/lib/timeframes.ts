/** Candle timeframe strings shared with the backend. */

export const TIMEFRAME_SECONDS: Record<string, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '4h': 14_400,
  '1d': 86_400,
}

const DEFAULT_TIMEFRAME_SECONDS = 3600

export function timeframeToSeconds(timeframe: string): number {
  return TIMEFRAME_SECONDS[timeframe] ?? DEFAULT_TIMEFRAME_SECONDS
}

/**
 * GET /api/v1/candles filters `ts < end`, while equity snapshots carry the processed
 * candle's own ts — so the run's final candle would be excluded. Extend the end of
 * the range by one timeframe interval to include it.
 */
export function candleQueryEnd(lastEquityTs: string, timeframe: string): string {
  return new Date(Date.parse(lastEquityTs) + timeframeToSeconds(timeframe) * 1000).toISOString()
}
