import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CappedNotice } from './CappedNotice'

describe('CappedNotice', () => {
  it('shows a cap hint when the data length reaches the requested limit', () => {
    render(<CappedNotice length={50_000} limit={50_000} noun="points" />)
    expect(screen.getByText(/showing latest 50,000 points/i)).toBeInTheDocument()
  })

  it('renders nothing below the limit', () => {
    const { container } = render(<CappedNotice length={4_999} limit={50_000} noun="points" />)
    expect(container).toBeEmptyDOMElement()
  })
})
