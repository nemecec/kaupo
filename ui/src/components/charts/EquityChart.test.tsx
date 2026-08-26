import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { EquityChart } from './EquityChart'
import type { BenchmarkPoint, EquityPoint } from '../../lib/types'

const h = vi.hoisted(() => ({
  addSeries: vi.fn<(type: unknown, opts?: unknown) => { setData: ReturnType<typeof vi.fn> }>(() => ({
    setData: vi.fn(),
  })),
}))

// jsdom has no canvas: fake the chart and capture the added series
vi.mock('lightweight-charts', () => ({
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
  AreaSeries: 'AreaSeries',
  LineSeries: 'LineSeries',
  LineStyle: { Dashed: 2 },
  createSeriesMarkers: vi.fn(),
  createChart: () => ({
    addSeries: h.addSeries,
    removeSeries: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: vi.fn(),
  }),
}))

const POINTS: EquityPoint[] = [
  { ts: '2026-01-01T00:00:00Z', equity: 10_000, cash: 10_000, unrealized_pnl: 0 },
  { ts: '2026-01-01T01:00:00Z', equity: 10_100, cash: 10_100, unrealized_pnl: 0 },
]

const BENCHMARK: BenchmarkPoint[] = [
  { ts: '2026-01-01T00:00:00Z', value: 10_000 },
  { ts: '2026-01-01T01:00:00Z', value: 10_050 },
]

describe('EquityChart', () => {
  beforeEach(() => {
    h.addSeries.mockClear()
  })

  it('draws the benchmark as a dashed line with a "buy & hold" label', () => {
    render(<EquityChart points={POINTS} benchmark={BENCHMARK} />)

    expect(h.addSeries).toHaveBeenCalledTimes(2)
    // benchmark first (equity area draws over it), dashed and neutral
    const benchCall = h.addSeries.mock.calls[0]
    expect(benchCall?.[0]).toBe('LineSeries')
    expect(benchCall?.[1]).toMatchObject({ lineStyle: 2 })
    expect(h.addSeries.mock.calls[1]?.[0]).toBe('AreaSeries')
    expect(screen.getByText('buy & hold')).toBeInTheDocument()
  })

  it('draws only the equity area when no benchmark is given', () => {
    render(<EquityChart points={POINTS} />)

    expect(h.addSeries).toHaveBeenCalledTimes(1)
    expect(h.addSeries.mock.calls[0]?.[0]).toBe('AreaSeries')
    expect(screen.queryByText('buy & hold')).not.toBeInTheDocument()
  })

  it('treats an empty benchmark series as no benchmark', () => {
    render(<EquityChart points={POINTS} benchmark={[]} />)

    expect(h.addSeries).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('buy & hold')).not.toBeInTheDocument()
  })
})
