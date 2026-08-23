import { useEffect } from 'react'
import { CandlestickSeries, createSeriesMarkers } from 'lightweight-charts'
import type { Candle, Trade } from '../../lib/types'
import { useChart } from './useChart'
import { CHART_COLORS, candlesToBars, tradeMarkers } from './utils'

interface Props {
  candles: Candle[]
  trades?: Trade[]
  height?: number
}

/** OHLC candle chart with buy/sell markers snapped to bar times. */
export function CandleChart({ candles, trades = [], height = 340 }: Props) {
  const { containerRef, chartRef } = useChart(height)

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const series = chart.addSeries(CandlestickSeries, {
      upColor: CHART_COLORS.up,
      downColor: CHART_COLORS.down,
      wickUpColor: CHART_COLORS.up,
      wickDownColor: CHART_COLORS.down,
      borderVisible: false,
      priceLineVisible: false,
    })
    const bars = candlesToBars(candles)
    series.setData(bars)
    if (trades.length > 0 && bars.length > 0) {
      createSeriesMarkers(
        series,
        tradeMarkers(trades, bars.map((b) => b.time as number)),
      )
    }
    chart.timeScale().fitContent()
    return () => {
      chart.removeSeries(series)
    }
  }, [candles, trades, chartRef])

  return <div ref={containerRef} className="w-full" style={{ height }} data-testid="candle-chart" />
}
