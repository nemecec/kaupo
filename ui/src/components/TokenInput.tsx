import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { getToken, setToken } from '../lib/auth'

/** API bearer token input, persisted to localStorage; saving invalidates all queries. */
export function TokenInput() {
  const queryClient = useQueryClient()
  const [value, setValue] = useState(() => getToken() ?? '')
  const [flash, setFlash] = useState<string | null>(null)

  const apply = (token: string | null, message: string) => {
    setToken(token)
    void queryClient.invalidateQueries()
    setFlash(message)
    window.setTimeout(() => setFlash(null), 1500)
  }

  return (
    <div className="space-y-1.5">
      <label htmlFor="api-token" className="block text-xs font-medium text-zinc-500">
        API token
      </label>
      <input
        id="api-token"
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Bearer token (optional)"
        autoComplete="off"
        className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 placeholder-zinc-600 focus:border-accent focus:outline-none"
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => apply(value.trim() || null, 'Saved')}
          className="rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-white hover:bg-accent/85"
        >
          Save
        </button>
        <button
          type="button"
          onClick={() => {
            setValue('')
            apply(null, 'Cleared')
          }}
          className="rounded-md border border-zinc-700 px-2.5 py-1 text-xs text-zinc-400 hover:bg-zinc-800"
        >
          Clear
        </button>
        {flash && <span className="text-xs text-emerald-400">{flash}</span>}
      </div>
    </div>
  )
}
