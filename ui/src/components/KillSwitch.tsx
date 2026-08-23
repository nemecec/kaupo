import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/format'
import { ErrorState } from './common'

/**
 * Global kill switch: POST /api/v1/control/kill with run_id = null.
 * Admin-only on the backend — 401/403 responses surface as inline errors.
 */
export function KillSwitch() {
  const [confirming, setConfirming] = useState(false)
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => api.control('kill', null),
    onSuccess: () => {
      setConfirming(false)
      void queryClient.invalidateQueries()
    },
  })

  return (
    <div className="flex items-center gap-3">
      {mutation.isSuccess && mutation.data && (
        <span className="text-xs text-emerald-400">
          Kill issued {formatDateTime(mutation.data.issued_at)}
        </span>
      )}
      <button
        type="button"
        onClick={() => {
          mutation.reset()
          setConfirming(true)
        }}
        className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-sm font-medium text-rose-400 hover:bg-rose-500/20"
      >
        Kill switch
      </button>

      {confirming && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Confirm kill switch"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setConfirming(false)}
        >
          <div
            className="w-full max-w-sm rounded-lg border border-zinc-700 bg-zinc-900 p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold text-zinc-100">Halt all trading?</h3>
            <p className="mt-2 text-sm text-zinc-400">
              This issues a global <span className="font-mono text-rose-400">kill</span> command.
              Every running run will be halted. This action cannot be undone from the UI.
            </p>
            {mutation.isError && (
              <div className="mt-3">
                <ErrorState error={mutation.error} />
              </div>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={mutation.isPending}
                onClick={() => mutation.mutate()}
                className="rounded-md bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-500 disabled:opacity-50"
              >
                {mutation.isPending ? 'Killing…' : 'Confirm kill'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
