import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DailyMoney } from './DailyMoney'
import type { DailyReport } from '../lib/types'

const totals: DailyReport['totals'] = {
  num_runs: 2, active_runs: 2, total_fills: 0,
  total_pnl: null, total_fees: null, quote_currency: null,
  by_currency: {
    EUR: { total_pnl: 10, total_fees: 0.5, active_runs: 1, total_fills: 0 },
    USD: { total_pnl: -3, total_fees: 0.2, active_runs: 1, total_fills: 0 },
  },
  unclassified_runs: 0,
}

describe('DailyMoney', () => {
  it('shows separate currency totals without a combined amount', () => {
    render(<DailyMoney totals={totals} field="total_pnl" signed />)
    expect(screen.getByText('+10 EUR')).toBeInTheDocument()
    expect(screen.getByText('-3 USD')).toBeInTheDocument()
    expect(screen.queryByText('+7')).not.toBeInTheDocument()
  })

  it('labels fees separately too', () => {
    render(<DailyMoney totals={totals} field="total_fees" />)
    expect(screen.getByText('0.5 EUR')).toBeInTheDocument()
    expect(screen.getByText('0.2 USD')).toBeInTheDocument()
  })

  it('shows known subtotals and flags unclassified runs', () => {
    render(<DailyMoney totals={{ ...totals, unclassified_runs: 1 }} field="total_pnl" signed />)
    expect(screen.getByText('+10 EUR')).toBeInTheDocument()
    expect(screen.getByText(/currency unavailable for 1/i)).toBeInTheDocument()
  })

  it('does not invent a currency for legacy reports', () => {
    render(<DailyMoney totals={{ ...totals, by_currency: undefined, total_pnl: 999 }} field="total_pnl" />)
    expect(screen.getByText(/currency unavailable/i)).toBeInTheDocument()
    expect(screen.queryByText(/999/)).not.toBeInTheDocument()
  })

  it('renders zero on days with no monetary activity', () => {
    render(<DailyMoney totals={{ ...totals, by_currency: {} }} field="total_pnl" />)
    expect(screen.getByText('0')).toBeInTheDocument()
  })
})
