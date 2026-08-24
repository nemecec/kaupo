import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import { AuthGate } from './components/AuthGate'
import { Layout } from './components/Layout'
import Dashboard from './pages/Dashboard'
import RunsPage from './pages/RunsPage'
import RunDetailPage from './pages/RunDetailPage'
import BacktestPage from './pages/BacktestPage'
import ReportsPage from './pages/ReportsPage'
import EventsPage from './pages/EventsPage'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 5_000,
      refetchOnWindowFocus: false,
    },
  },
})

function NotFound() {
  return (
    <div className="py-16 text-center">
      <p className="text-lg text-zinc-400">Page not found</p>
      <Link to="/" className="mt-2 inline-block text-sm text-accent hover:underline">
        Back to dashboard
      </Link>
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route
            element={
              <AuthGate>
                <Layout />
              </AuthGate>
            }
          >
            <Route index element={<Dashboard />} />
            <Route path="runs" element={<RunsPage />} />
            <Route path="runs/:id" element={<RunDetailPage />} />
            <Route path="backtest" element={<BacktestPage />} />
            <Route path="reports" element={<ReportsPage />} />
            <Route path="events" element={<EventsPage />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
