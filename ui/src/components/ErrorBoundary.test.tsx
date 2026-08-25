import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

function Bomb(): never {
  throw new Error('boom')
}

describe('ErrorBoundary', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  it('shows a fallback instead of crashing the tree', () => {
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    )

    expect(screen.getByText(/something went wrong on this page/i)).toBeInTheDocument()
    expect(screen.getByText('boom')).toBeInTheDocument()
  })

  it('recatches when the child throws again after Try again', () => {
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(screen.getByText(/something went wrong on this page/i)).toBeInTheDocument()
  })
})
