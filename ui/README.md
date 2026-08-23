# Kaupo UI

Web frontend for Kaupo, the autonomous algorithmic crypto-trading system.
React 19 + TypeScript + Vite, TanStack Query, lightweight-charts, Tailwind CSS v4.

## Prerequisites

- Node 22+ and npm
- The Kaupo API running on `http://localhost:8000` (dev server proxies `/api` and `/ws` there)

## Commands

```bash
npm install        # install dependencies (CI: npm ci)
npm run dev        # dev server on http://localhost:5173, proxies /api and /ws to :8000
npm run lint       # eslint
npm run test       # vitest (jsdom + React Testing Library)
npm run build      # tsc -b && vite build -> dist/
npm run preview    # serve the production build locally
```

## Auth

If the API requires a bearer token, paste it into the **API token** field at the
bottom of the sidebar. It is stored in `localStorage` and sent as
`Authorization: Bearer <token>` on every `/api/*` request.

## Docker

```bash
docker build -t kaupo-ui .
docker run -p 8080:80 kaupo-ui   # expects an "api:8000" host for /api and /ws (docker-compose network)
```

The image is multi-stage: `node:22-alpine` builds the static bundle, `nginx:alpine`
serves it and reverse-proxies `/api` and `/ws` to `api:8000` (see `nginx.conf`).

## Layout

- `src/lib/` — API client, types, auth token, formatting helpers
- `src/hooks/queries.ts` — TanStack Query hooks (polling intervals live here)
- `src/components/` — layout, charts (`components/charts/`), tables, kill switch
- `src/pages/` — Dashboard, Runs, Run detail, Backtest, Reports, Events
- `src/test/` — test setup and render helpers
