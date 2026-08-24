import { describe, expect, it } from 'vitest'
import { candleQueryEnd, timeframeToSeconds } from './timeframes'

describe('timeframeToSeconds', () => {
  it('maps known timeframe strings to seconds', () => {
    expect(timeframeToSeconds('1m')).toBe(60)
    expect(timeframeToSeconds('5m')).toBe(300)
    expect(timeframeToSeconds('15m')).toBe(900)
    expect(timeframeToSeconds('30m')).toBe(1800)
    expect(timeframeToSeconds('1h')).toBe(3600)
    expect(timeframeToSeconds('4h')).toBe(14_400)
    expect(timeframeToSeconds('1d')).toBe(86_400)
  })

  it('defaults to 1h for unknown timeframes', () => {
    expect(timeframeToSeconds('7h')).toBe(3600)
    expect(timeframeToSeconds('')).toBe(3600)
  })
})

describe('candleQueryEnd', () => {
  it('extends the end by one interval so the final candle passes the ts < end filter', () => {
    expect(candleQueryEnd('2026-08-24T10:00:00Z', '1h')).toBe('2026-08-24T11:00:00.000Z')
    expect(candleQueryEnd('2026-08-24T10:00:00Z', '15m')).toBe('2026-08-24T10:15:00.000Z')
    expect(candleQueryEnd('2026-08-24T10:00:00Z', '1d')).toBe('2026-08-25T10:00:00.000Z')
  })
})
