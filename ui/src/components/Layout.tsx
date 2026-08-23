import { NavLink, Outlet } from 'react-router-dom'
import { TokenInput } from './TokenInput'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/runs', label: 'Runs', end: false },
  { to: '/backtest', label: 'Backtest', end: false },
  { to: '/reports', label: 'Reports', end: false },
  { to: '/events', label: 'Events', end: false },
]

export function Layout() {
  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-200">
      {/* Estonian flag bar, per the Kaupo design doc */}
      <div
        className="h-1 w-full"
        style={{ background: 'linear-gradient(to bottom, #0072ce 0 33.3%, #000 33.3% 66.6%, #fff 66.6% 100%)' }}
      />
      <div className="flex">
        <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col border-r border-zinc-800 bg-zinc-900/40">
          <div className="border-b border-zinc-800 px-4 py-4">
            <span className="text-xl font-bold tracking-wide text-white">
              Kaupo<span className="text-accent">.</span>
            </span>
            <p className="mt-0.5 text-xs text-zinc-500">algorithmic trading</p>
          </div>
          <nav className="flex-1 space-y-0.5 overflow-y-auto px-2 py-3">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `block rounded-md px-3 py-2 text-sm ${
                    isActive
                      ? 'bg-accent/15 font-medium text-accent'
                      : 'text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200'
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="border-t border-zinc-800 p-3">
            <TokenInput />
          </div>
        </aside>
        <main className="min-w-0 flex-1 px-6 py-6">
          <div className="mx-auto max-w-6xl">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
