import { useEffect, useRef } from 'react'
import {
  AreaSeries,
  createSeriesMarkers,
  LineSeries,
  LineStyle,
  type ISeriesApi,
  type SeriesMarker,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { BenchmarkPoint, EquityPoint } from '../../lib/types'
import { useChart } from './useChart'
import { benchmarkToLine, CHART_COLORS, equityToLine } from './utils'

interface Props {
  points: EquityPoint[]
  /** Buy-and-hold reference line; omitted/empty draws only the equity area. */
  benchmark?: BenchmarkPoint[]
  markers?: SeriesMarker<UTCTimestamp>[]
  height?: number
}

/** Single equity-curve area chart, optionally with a buy-and-hold benchmark line and trade markers. */
export function EquityChart({ points, benchmark, markers, height = 280 }: Props) {
  const { containerRef, chartRef, disposeSeries } = useChart(height)
  // fit the time scale only on first data load so live updates keep the user's zoom
  const fittedRef = useRef(false)
  const hasBenchmark = benchmark !== undefined && benchmark.length > 0

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    // benchmark first so the equity area draws over it
    let benchSeries: ISeriesApi<'Line'> | null = null
    if (benchmark && benchmark.length > 0) {
      benchSeries = chart.addSeries(LineSeries, {
        color: CHART_COLORS.benchmark,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerRadius: 3,
      })
      benchSeries.setData(benchmarkToLine(benchmark))
    }
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
      if (benchSeries) disposeSeries(benchSeries)
    }
  }, [points, benchmark, markers, chartRef, disposeSeries])

  return (
    <div>
      <div ref={containerRef} className="w-full" style={{ height }} data-testid="equity-chart" />
      {hasBenchmark && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
          <span className="flex items-center gap-1.5 text-xs text-zinc-400">
            <span
              className="inline-block w-4 self-center border-t-2 border-dashed"
              style={{ borderColor: CHART_COLORS.benchmark }}
            />
            buy & hold
          </span>
        </div>
      )}
    </div>
  )
}
