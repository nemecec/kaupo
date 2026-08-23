import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MetricCards } from './MetricCards'

describe('MetricCards', () => {
  it('renders metric keys as labels and formats values', () => {
    render(
      <MetricCards metrics={{ total_return_pct: 12.3456, num_fills: 42, sharpe: 1.5 }} />,
    )
    expect(screen.getByText('total return pct')).toBeInTheDocument()
    expect(screen.getByText('12.35%')).toBeInTheDocument()
    expect(screen.getByText('num fills')).toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
    expect(screen.getByText('sharpe')).toBeInTheDocument()
    expect(screen.getByText('1.5')).toBeInTheDocument()
  })

  it('shows an empty state when there are no metrics', () => {
    render(<MetricCards metrics={null} />)
    expect(screen.getByText(/no metrics recorded/i)).toBeInTheDocument()
  })
})
