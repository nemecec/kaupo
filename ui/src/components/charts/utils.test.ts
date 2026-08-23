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

  it('maps sides to arrow markers', () => {
    const markers = tradeMarkers([buy, { ...buy, id: 't2', side: 'sell' }])
    expect(markers[0]).toMatchObject({ shape: 'arrowUp', position: 'belowBar' })
    expect(markers[1]).toMatchObject({ shape: 'arrowDown', position: 'aboveBar' })
  })

  it('snaps markers to the nearest bar time', () => {
    const barTime = Math.floor(new Date('2026-08-24T10:00:00Z').getTime() / 1000)
    const markers = tradeMarkers([buy], [barTime])
    expect(markers).toHaveLength(1)
    expect(markers[0].time).toBe(barTime)
  })
})
