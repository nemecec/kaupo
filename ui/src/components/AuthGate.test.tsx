import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthGate } from './AuthGate'
import { setToken } from '../lib/auth'

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

function jsonResponse(status: number): Response {
  return new Response('{}', { status })
}

function renderGate() {
  return render(
    <AuthGate>
      <div>secret content</div>
    </AuthGate>,
  )
}

describe('AuthGate', () => {
  beforeEach(() => {
    localStorage.clear()
    fetchMock.mockReset()
  })

  it('shows only the token prompt when the API demands auth', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401))
    renderGate()

    expect(await screen.findByLabelText('API token')).toBeInTheDocument()
    expect(screen.queryByText('secret content')).not.toBeInTheDocument()
  })

  it('renders children when the API allows anonymous access (auth disabled)', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200))
    renderGate()

    expect(await screen.findByText('secret content')).toBeInTheDocument()
    expect(screen.queryByLabelText('API token')).not.toBeInTheDocument()
  })

  it('renders children when the stored token is valid', async () => {
    setToken('stored-token')
    fetchMock.mockResolvedValue(jsonResponse(200))
    renderGate()

    expect(await screen.findByText('secret content')).toBeInTheDocument()
  })

  it('rejects an invalid token with an error message', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401))
    renderGate()

    fireEvent.change(await screen.findByLabelText('API token'), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid token.')
    expect(screen.queryByText('secret content')).not.toBeInTheDocument()
  })

  it('accepts a valid token and renders children', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401)) // initial probe
    renderGate()

    fireEvent.change(await screen.findByLabelText('API token'), { target: { value: 'good' } })
    fetchMock.mockResolvedValue(jsonResponse(200)) // validation + re-probe
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByText('secret content')).toBeInTheDocument()
    expect(localStorage.getItem('kaupo.api.token')).toBe('good')
  })

  it('returns to the token prompt when the token is cleared', async () => {
    setToken('stored-token')
    fetchMock.mockResolvedValue(jsonResponse(200))
    renderGate()
    expect(await screen.findByText('secret content')).toBeInTheDocument()

    fetchMock.mockResolvedValue(jsonResponse(401))
    setToken(null)

    expect(await screen.findByLabelText('API token')).toBeInTheDocument()
    expect(screen.queryByText('secret content')).not.toBeInTheDocument()
  })

  it('shows an unreachable state with retry when the API is down', async () => {
    fetchMock.mockRejectedValue(new TypeError('network down'))
    renderGate()

    expect(await screen.findByText('Cannot reach the API.')).toBeInTheDocument()
    expect(screen.queryByText('secret content')).not.toBeInTheDocument()

    fetchMock.mockResolvedValue(jsonResponse(200))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('secret content')).toBeInTheDocument()
  })
})
