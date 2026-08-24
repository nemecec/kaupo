import { describe, expect, it } from 'vitest'
import { drawdownLine, tradeMarkers } from './utils'
import type { EquityPoint, Trade } from '../../lib/types'

function equity(ts: string, value: number): EquityPoint {
  return { ts, equity: value, cash: value, unrealized_pnl: 0 }
}

describe('drawdownLine', () => {
  it('computes drawdown percent relative to the running peak', () => {
    const dd = drawdownLine([
      equity('2026-08-24T10:00:00Z', 100),
      equity('2026-08-24T11:00:00Z', 120),
      equity('2026-08-24T12:00:00Z', 90),
    ])
    expect(dd.map((p) => p.value)).toEqual([0, 0, -25])
  })

  it('deduplicates identical timestamps keeping the last value', () => {
    const dd = drawdownLine([
      equity('2026-08-24T10:00:00Z', 100),
      equity('2026-08-24T10:00:00Z', 110),
    ])
    expect(dd).toHaveLength(1)
    expect(dd[0].value).toBe(0)
  })
})

describe('tradeMarkers', () => {
  const buy: Trade = {
    id: 't1',
    order_id: 'o1',
    ts: '2026-08-24T10:03:00Z',
    pair: 'BTC/EUR',
    side: 'buy',
    price: 100,
    size: 0.5,
    fee: 0.1,
  }

  const toSec = (iso: string) => Math.floor(new Date(iso).getTime() / 1000)
  const bars = [toSec('2026-08-24T10:00:00Z'), toSec('2026-08-24T11:00:00Z')]

  it('maps sides to arrow markers', () => {
    const markers = tradeMarkers([buy, { ...buy, id: 't2', side: 'sell' }])
    expect(markers[0]).toMatchObject({ shape: 'arrowUp', position: 'belowBar' })
    expect(markers[1]).toMatchObject({ shape: 'arrowDown', position: 'aboveBar' })
  })

  it('snaps in-range markers to the nearest bar time', () => {
    const markers = tradeMarkers([buy], bars) // 10:03 -> nearest bar is 10:00
    expect(markers).toHaveLength(1)
    expect(markers[0].time).toBe(bars[0])
  })

  it('skips markers outside the loaded bar range', () => {
    const early = { ...buy, id: 't0', ts: '2026-08-24T09:00:00Z' }
    const late = { ...buy, id: 't3', ts: '2026-08-24T12:00:00Z' }
    const markers = tradeMarkers([early, buy, late], bars)
    expect(markers).toHaveLength(1)
    expect(markers[0].time).toBe(bars[0])
    expect(tradeMarkers([early], bars)).toHaveLength(0)
    expect(tradeMarkers([late], bars)).toHaveLength(0)
  })

  it('returns no markers when no bars are loaded', () => {
    expect(tradeMarkers([buy], [])).toHaveLength(0)
  })
})
