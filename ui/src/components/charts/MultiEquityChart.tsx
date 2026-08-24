import { useEffect, useRef } from 'react'
import { LineSeries } from 'lightweight-charts'
import { useChart } from './useChart'
import type { LinePoint } from './utils'

export interface NamedSeries {
  name: string
  color: string
  points: LinePoint[]
}

/** Overlaid equity lines for several runs (dashboard). */
export function MultiEquityChart({ seriesList, height = 300 }: { seriesList: NamedSeries[]; height?: number }) {
  const { containerRef, chartRef } = useChart(height)
  // fit the time scale only on first data load so live updates keep the user's zoom
  const fittedRef = useRef(false)

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const added = seriesList.map((s) => {
      const series = chart.addSeries(LineSeries, {
        color: s.color,
        lineWidth: 2,
        priceLineVisible: false,
        crosshairMarkerRadius: 3,
      })
      series.setData(s.points)
      return series
    })
    if (!fittedRef.current && added.length > 0) {
      chart.timeScale().fitContent()
      fittedRef.current = true
    }
    return () => {
      for (const series of added) chart.removeSeries(series)
    }
  }, [seriesList, chartRef])

  return (
    <div>
      <div ref={containerRef} className="w-full" style={{ height }} data-testid="multi-equity-chart" />
      {seriesList.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
          {seriesList.map((s) => (
            <span key={s.name} className="flex items-center gap-1.5 text-xs text-zinc-400">
              <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: s.color }} />
              <span className="font-mono">{s.name}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
