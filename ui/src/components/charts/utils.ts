import {
  ColorType,
  CrosshairMode,
  type CandlestickData,
  type ChartOptions,
  type DeepPartial,
  type SeriesMarker,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { Candle, EquityPoint, Trade } from '../../lib/types'

export const CHART_COLORS = {
  accent: '#0072ce',
  up: '#10b981',
  down: '#f43f5e',
  text: '#a1a1aa',
} as const

export const BASE_CHART_OPTIONS: DeepPartial<ChartOptions> = {
  autoSize: true,
  layout: {
    background: { type: ColorType.Solid, color: 'transparent' },
    textColor: CHART_COLORS.text,
    fontSize: 11,
  },
  grid: {
    vertLines: { color: 'rgba(255, 255, 255, 0.05)' },
    horzLines: { color: 'rgba(255, 255, 255, 0.05)' },
  },
  rightPriceScale: { borderColor: 'rgba(255, 255, 255, 0.12)' },
  timeScale: { borderColor: 'rgba(255, 255, 255, 0.12)', timeVisible: true, secondsVisible: false },
  crosshair: { mode: CrosshairMode.Normal },
}

export function isoToTs(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp
}

export interface LinePoint {
  time: UTCTimestamp
  value: number
}

/** Sorted, time-deduplicated line series — lightweight-charts requires strictly ascending times. */
function toSortedUniqueLine<T extends { ts: string }>(
  points: T[],
  pick: (p: T) => number,
): LinePoint[] {
  const sorted = [...points].sort((a, b) => a.ts.localeCompare(b.ts))
  const out: LinePoint[] = []
  for (const p of sorted) {
    const time = isoToTs(p.ts)
    const value = pick(p)
    if (out.length > 0 && out[out.length - 1].time === time) {
      out[out.length - 1] = { time, value }
    } else {
      out.push({ time, value })
    }
  }
  return out
}

export function equityToLine(points: EquityPoint[]): LinePoint[] {
  return toSortedUniqueLine(points, (p) => p.equity)
}

/** Drawdown in % relative to the running equity peak (always <= 0). */
export function drawdownLine(points: EquityPoint[]): LinePoint[] {
  const equity = toSortedUniqueLine(points, (p) => p.equity)
  let peak = -Infinity
  return equity.map((p) => {
    peak = Math.max(peak, p.value)
    const dd = peak > 0 ? ((p.value - peak) / peak) * 100 : 0
    return { time: p.time, value: dd }
  })
}

export function candlesToBars(candles: Candle[]): CandlestickData<UTCTimestamp>[] {
  const sorted = [...candles].sort((a, b) => a.ts.localeCompare(b.ts))
  const out: CandlestickData<UTCTimestamp>[] = []
  for (const c of sorted) {
    const time = isoToTs(c.ts)
    const bar = { time, open: c.open, high: c.high, low: c.low, close: c.close }
    if (out.length > 0 && out[out.length - 1].time === time) {
      out[out.length - 1] = bar
    } else {
      out.push(bar)
    }
  }
  return out
}

/** Nearest time in a sorted ascending array, or null when empty. */
function snapToNearest(times: readonly number[], target: number): number | null {
  if (times.length === 0) return null
  let lo = 0
  let hi = times.length - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] < target) lo = mid + 1
    else hi = mid
  }
  if (lo > 0 && Math.abs(times[lo - 1] - target) <= Math.abs(times[lo] - target)) {
    return times[lo - 1]
  }
  return times[lo]
}

/**
 * Trade markers for a chart series. When `seriesTimes` (ascending) is given, each marker
 * is snapped to the nearest bar time so it actually renders on that series.
 */
export function tradeMarkers(
  trades: Trade[],
  seriesTimes?: readonly number[],
): SeriesMarker<UTCTimestamp>[] {
  const sorted = [...trades].sort((a, b) => a.ts.localeCompare(b.ts))
  const markers: SeriesMarker<UTCTimestamp>[] = []
  for (const t of sorted) {
    const raw = isoToTs(t.ts) as number
    const snapped = seriesTimes ? snapToNearest(seriesTimes, raw) : raw
    if (snapped === null) continue
    markers.push({
      time: snapped as UTCTimestamp,
      position: t.side === 'buy' ? 'belowBar' : 'aboveBar',
      shape: t.side === 'buy' ? 'arrowUp' : 'arrowDown',
      color: t.side === 'buy' ? CHART_COLORS.up : CHART_COLORS.down,
      text: t.side === 'buy' ? 'B' : 'S',
      size: 1,
    })
  }
  return markers
}
