# Bitcon-Trads Operations Dashboard — Frontend

A read-only operations terminal for the spot trend-following bot. Vite + React 18 +
TypeScript (strict) + Tailwind + shadcn/ui patterns + TanStack Query/Table + Recharts.

It talks to the FastAPI backend in `../` (the `web/` package) over REST + SSE. The
dashboard **never places or modifies orders**; the only mutation it can make is
adjusting the deployable-capital limit, which routes through the backend's audited
settings service.

## Quick start (local dev)

```bash
# 1) Start the backend (from the repo root, in another terminal)
pip install -r requirements-web.txt
python -m web.main                 # serves the API on http://localhost:8080

# 2) Start the frontend dev server (from this folder)
npm install
npm run dev                        # http://localhost:5173
```

`vite dev` proxies `/api` → `http://localhost:8080`, so the SPA talks to a
same-origin URL and there is no CORS to configure in dev. Point the proxy elsewhere
with `VITE_DEV_API_TARGET` (see `.env.example`).

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server with HMR + `/api` proxy + SSE pass-through |
| `npm run build` | `tsc --noEmit` (strict typecheck) then `vite build` → `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm run typecheck` | Strict TypeScript check only |

## Production / Railway

In production the SPA is served **by FastAPI** from `web/frontend/dist/` (StaticFiles),
so the API and UI are same-origin — no proxy, no CORS. The repo's multi-stage
`Dockerfile` builds this folder and copies `dist/` into the image; the backend mounts
it automatically if present (`web/server.py` → `_FRONTEND_DIST`). Enable the dashboard
process with `RUN_BOTS=spot,web`. See `docs/DASHBOARD_ARCHITECTURE.md` §11.

## Architecture notes

- **Data fetching** — TanStack Query, one hook per endpoint (`src/hooks/queries.ts`).
  Every query keeps a `refetchInterval` as a polling fallback.
- **Real-time** — a single `EventSource` to `/api/stream` (`src/hooks/useStream.tsx`)
  pushes `summary_update`, `positions_update`, `new_trade`, `new_decision`,
  `risk_alert`, and `equity_update` straight into the Query cache. If SSE drops, the
  UI degrades to polling and the connection indicator shows "Polling".
- **Types** — `src/types/api.ts` mirrors the backend Pydantic models exactly. The
  backend also serves OpenAPI at `/api/openapi.json` if you prefer `openapi-typescript`.
- **Auth** — optional. If the backend sets `DASHBOARD_TOKEN`, supply it via
  `VITE_DASHBOARD_TOKEN` at build time (dev) or wire a runtime prompt to
  `setAuthToken()` in `src/lib/api.ts`. It is sent as `X-API-Key`. The capital PUT is
  fail-closed server-side.
- **Theme** — dark by default; tokens in `src/index.css`, toggled via `useTheme`.
- **Accessibility** — Radix primitives (focus management, ARIA), keyboard-reachable
  tables/rows, color + icon + sign for all P&L, `prefers-reduced-motion` honored.
- **Performance** — route- and chart-level code splitting (`React.lazy`), skeletons
  on first load, `recharts`/`vendor` isolated chunks, tabular-num alignment.

## Folder structure

```
src/
├── main.tsx, App.tsx          # entry + router (lazy routes)
├── index.css                  # theme tokens (dark trading palette)
├── types/api.ts               # backend contract (mirrors web/models.py)
├── lib/                       # api client, query keys, glossary, utils
├── hooks/                     # queries, useStream (SSE), useTheme
├── components/ui/             # shadcn-style primitives
├── components/trading/        # PnLBadge, RiskGauge, MetricCard, PositionCard, …
├── components/charts/         # EquityChart, Sparkline, Attribution, Allocation
├── components/layout/         # AppShell, Topbar, Sidebar
└── pages/                     # Overview, Positions, History, Decisions,
                               # Performance, Risk, Config, Health, Strategy
```

## Adding a shadcn/ui component

`components.json` is configured (style: default, base color: zinc, CSS variables).
You can `npx shadcn@latest add <component>` to drop new primitives into
`src/components/ui`, or hand-write them following the existing files.
