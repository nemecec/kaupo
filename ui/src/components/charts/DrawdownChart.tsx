import { useEffect, useRef } from 'react'
import { AreaSeries } from 'lightweight-charts'
import type { EquityPoint } from '../../lib/types'
import { useChart } from './useChart'
import { CHART_COLORS, drawdownLine } from './utils'

/** Underwater equity (drawdown %) chart, always <= 0. */
export function DrawdownChart({ points, height = 160 }: { points: EquityPoint[]; height?: number }) {
  const { containerRef, chartRef } = useChart(height)
  // fit the time scale only on first data load so live updates keep the user's zoom
  const fittedRef = useRef(false)

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const series = chart.addSeries(AreaSeries, {
      lineColor: CHART_COLORS.down,
      topColor: 'rgba(244, 63, 94, 0.02)',
      bottomColor: 'rgba(244, 63, 94, 0.3)',
      lineWidth: 1,
      priceLineVisible: false,
      priceFormat: { type: 'percent' },
    })
    const data = drawdownLine(points)
    series.setData(data)
    if (!fittedRef.current && data.length > 0) {
      chart.timeScale().fitContent()
      fittedRef.current = true
    }
    return () => {
      chart.removeSeries(series)
    }
  }, [points, chartRef])

  return <div ref={containerRef} className="w-full" style={{ height }} data-testid="drawdown-chart" />
}
