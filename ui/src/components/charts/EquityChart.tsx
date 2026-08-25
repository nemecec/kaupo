import { useEffect, useRef } from 'react'
import { AreaSeries, createSeriesMarkers, type SeriesMarker, type UTCTimestamp } from 'lightweight-charts'
import type { EquityPoint } from '../../lib/types'
import { useChart } from './useChart'
import { CHART_COLORS, equityToLine } from './utils'

interface Props {
  points: EquityPoint[]
  markers?: SeriesMarker<UTCTimestamp>[]
  height?: number
}

/** Single equity-curve area chart, optionally with trade markers. */
export function EquityChart({ points, markers, height = 280 }: Props) {
  const { containerRef, chartRef, disposeSeries } = useChart(height)
  // fit the time scale only on first data load so live updates keep the user's zoom
  const fittedRef = useRef(false)

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const series = chart.addSeries(AreaSeries, {
      lineColor: CHART_COLORS.accent,
      topColor: 'rgba(0, 114, 206, 0.28)',
      bottomColor: 'rgba(0, 114, 206, 0.02)',
      lineWidth: 2,
      priceLineVisible: false,
      crosshairMarkerRadius: 3,
    })
    const data = equityToLine(points)
    series.setData(data)
    if (markers && markers.length > 0) {
      createSeriesMarkers(series, markers)
    }
    if (!fittedRef.current && data.length > 0) {
      chart.timeScale().fitContent()
      fittedRef.current = true
    }
    return () => {
      disposeSeries(series)
    }
  }, [points, markers, chartRef, disposeSeries])

  return <div ref={containerRef} className="w-full" style={{ height }} data-testid="equity-chart" />
}
