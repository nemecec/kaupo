import { useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { api, ApiError, validateToken } from '../lib/api'
import { setToken, TOKEN_CHANGED_EVENT } from '../lib/auth'

type GateState = 'loading' | 'allowed' | 'required' | 'unreachable'

// Estonian flag bar, per the Kaupo design doc
const FLAG_BAR = 'linear-gradient(to bottom, #0072ce 0 33.3%, #000 33.3% 66.6%, #fff 66.6% 100%)'

function TokenGate() {
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const token = value.trim()
    if (!token || busy) return
    setBusy(true)
    setError(null)
    const result = await validateToken(token)
    setBusy(false)
    if (result === 'ok') {
      setToken(token) // flips the gate through TOKEN_CHANGED_EVENT
      return
    }
    setError(result === 'invalid' ? 'Invalid token.' : 'Cannot reach the API. Try again.')
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-200">
      <div className="h-1 w-full" style={{ background: FLAG_BAR }} />
      <div className="flex min-h-[calc(100vh-4px)] items-center justify-center px-4">
        <form onSubmit={(e) => void submit(e)} className="w-full max-w-xs space-y-4">
          <div className="text-center">
            <span className="text-xl font-bold tracking-wide text-white">
              Kaupo<span className="text-accent">.</span>
            </span>
            <p className="mt-1 text-sm text-zinc-500">Enter your API token to continue.</p>
          </div>
          <input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="API token"
            aria-label="API token"
            autoComplete="off"
            autoFocus
            className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:border-accent focus:outline-none"
          />
          {error && (
            <p role="alert" className="text-sm text-red-400">
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy || !value.trim()}
            className="w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/85 disabled:opacity-50"
          >
            {busy ? 'Checking…' : 'Continue'}
          </button>
        </form>
      </div>
    </div>
  )
}

function ApiUnreachable({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-200">
      <div className="h-1 w-full" style={{ background: FLAG_BAR }} />
      <div className="flex min-h-[calc(100vh-4px)] flex-col items-center justify-center gap-3 px-4">
        <p className="text-sm text-zinc-400">Cannot reach the API.</p>
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          Retry
        </button>
      </div>
    </div>
  )
}

/**
 * Renders children only when the API accepts the current credentials. One probe
 * decides: when the API allows anonymous access (local development, auth
 * disabled), render the app; when it demands a token, show a bare token prompt
 * and nothing else. Clearing the token returns to the prompt.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>('loading')
  const [probeTick, setProbeTick] = useState(0)

  useEffect(() => {
    const onTokenChanged = () => setProbeTick((tick) => tick + 1)
    window.addEventListener(TOKEN_CHANGED_EVENT, onTokenChanged)
    return () => window.removeEventListener(TOKEN_CHANGED_EVENT, onTokenChanged)
  }, [])

  useEffect(() => {
    let cancelled = false
    api
      .status()
      .then(() => {
        if (!cancelled) setState('allowed')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          setState('required')
        } else {
          setState('unreachable')
        }
      })
    return () => {
      cancelled = true
    }
  }, [probeTick])

  if (state === 'loading') {
    return <div className="min-h-screen bg-zinc-950" />
  }
  if (state === 'required') {
    return <TokenGate />
  }
  if (state === 'unreachable') {
    return <ApiUnreachable onRetry={() => setProbeTick((tick) => tick + 1)} />
  }
  return children
}
