import { useCallback, useEffect, useRef, type RefObject } from 'react'
import { createChart, type IChartApi, type ISeriesApi, type SeriesType } from 'lightweight-charts'
import { BASE_CHART_OPTIONS } from './utils'

/**
 * Creates a lightweight-charts chart bound to a container div.
 * The chart auto-sizes with its container and is removed on unmount.
 */
export function useChart(height: number): {
  containerRef: RefObject<HTMLDivElement | null>
  chartRef: RefObject<IChartApi | null>
  disposeSeries: (series: ISeriesApi<SeriesType>) => void
} {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, { ...BASE_CHART_OPTIONS, height })
    chartRef.current = chart
    return () => {
      chart.remove()
      chartRef.current = null
    }
  }, [height])

  // Effect cleanups run in declaration order, so by series-cleanup time the
  // chart may already be disposed. This guard reads the ref late, on purpose.
  const disposeSeries = useCallback((series: ISeriesApi<SeriesType>) => {
    chartRef.current?.removeSeries(series)
  }, [])

  return { containerRef, chartRef, disposeSeries }
}
