import type { ReactNode } from 'react'

export function Panel({
  title,
  action,
  children,
}: {
  title?: ReactNode
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60">
      {(title || action) && (
        <header className="flex items-center justify-between gap-4 border-b border-zinc-800 px-4 py-2.5">
          <h2 className="text-sm font-medium text-zinc-300">{title}</h2>
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Loading({ text = 'Loading…' }: { text?: string }) {
  return <div className="py-8 text-center text-sm text-zinc-500">{text}</div>
}

export function EmptyState({ text }: { text: string }) {
  return <div className="py-8 text-center text-sm text-zinc-500">{text}</div>
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}

export function ErrorState({ error }: { error: unknown }) {
  return (
    <div role="alert" className="rounded-md border border-rose-900/60 bg-rose-950/40 px-3 py-2 text-sm text-rose-300">
      {errorMessage(error)}
    </div>
  )
}
