# GramVault Atlas — frontend

React + TypeScript + Vite single-page app for browsing, searching, and
chatting over an imported GramVault library. Talks to the FastAPI backend
in `../backend` over `/api/*` and `/media/*`.

## Development

```
npm install
npm run dev        # Vite dev server against a running backend (see
                    # GRAMVAULT_DEV=1 in ../backend/gramvault/main.py)
```

## Scripts

- `npm run dev` — Vite dev server with HMR.
- `npm run build` — type-check (`tsc -b`) then production build to `dist/`,
  which `gramvault serve` picks up and serves as static files.
- `npm run lint` — oxlint.
- `npm test` / `npm run test:watch` — vitest + React Testing Library.
- `npm run gen:api` — regenerate `src/api/schema.d.ts` from
  `openapi.json` (itself produced by `gramvault openapi`, run from the
  backend). CI's `api-types` job fails if either committed file drifts
  from what regenerating them produces — run this after any backend API
  change.

## Layout

- `src/api/client.ts` — fetch/XHR/SSE wrapper: auth header injection,
  `ApiError`, `mediaUrl()`, `uploadFile()`, `streamChatMessage()`.
- `src/lib/` — shared logic used across pages (gallery filtering/sibling
  navigation, citation rendering).
- `src/components/` — shared UI (including `AuthGate`, the bearer-token
  prompt shown when the backend requires auth).
- `src/pages/` — one module per route.
