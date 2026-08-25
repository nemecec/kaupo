import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { UTCTimestamp } from 'lightweight-charts'
import { MultiEquityChart } from './MultiEquityChart'

// Fake chart that fails hard when used after disposal, like lightweight-charts.
const h = vi.hoisted(() => ({
  disposed: false,
  removeSeries: vi.fn(),
  remove: vi.fn(),
}))

vi.mock('lightweight-charts', () => ({
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
  LineSeries: 'LineSeries',
  createChart: () => ({
    addSeries: () => ({ setData: vi.fn() }),
    removeSeries: (series: unknown) => {
      h.removeSeries(series)
      if (h.disposed) throw new Error('removeSeries on a disposed chart')
    },
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: () => {
      h.remove()
      h.disposed = true
    },
  }),
}))

const SERIES = [
  { name: 'run-1', color: '#0072ce', points: [{ time: 1 as UTCTimestamp, value: 100 }] },
]

describe('MultiEquityChart', () => {
  beforeEach(() => {
    h.disposed = false
    h.removeSeries.mockClear()
    h.remove.mockClear()
  })

  it('unmount disposes the chart without touching it afterwards', () => {
    const { unmount } = render(<MultiEquityChart seriesList={SERIES} />)

    // React runs cleanups in declaration order: the chart is disposed first,
    // so the series cleanup must skip instead of calling a dead chart.
    expect(() => unmount()).not.toThrow()
    expect(h.remove).toHaveBeenCalledTimes(1)
    expect(h.removeSeries).not.toHaveBeenCalled()
  })
})
