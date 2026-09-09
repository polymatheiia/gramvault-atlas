# Contributing to GramVault Atlas

Thanks for considering a contribution. This is a small, local-first project — the bar is "it works, it's tested, and it doesn't leak data off the user's machine."

GramVault Atlas is a fork of [GramVault](https://github.com/aleksanderislami03-cell/gramvault); upstream bug fixes are welcome here, and fixes that aren't fork-specific are worth sending upstream too.

## Dev environment setup

Prerequisites: Python 3.11+, Node 20.19+ (22 LTS recommended), ffmpeg on `PATH`, and Ollama installed locally.

```bash
git clone https://github.com/polymatheiia/gramvault-atlas.git
cd gramvault-atlas

./setup.sh          # macOS / Linux
./setup.ps1         # Windows (PowerShell)
```

This creates a `.venv`, installs the backend editable with dev extras (`pip install -e ".[dev]"`), and installs + builds the frontend. By hand:

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows:     .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# add ".[instagram]" too if you're working on the opt-in pull feature

cd frontend && npm install && npm run build && cd ..
```

For frontend hot reload, run `npm run dev` inside `frontend/` (Vite on port 5173) alongside `gramvault serve` — the backend's CORS already allows the Vite origin.

## Running tests and linters

Backend (repo root, venv active):

```bash
pytest backend/tests
ruff check .
```

Frontend (`frontend/`):

```bash
npm run build   # tsc -b && vite build — also a type-check
npm run lint    # oxlint
```

All of these run in CI on every push and PR (`.github/workflows/ci.yml`). AI calls (Ollama, ffmpeg, faster-whisper) are mocked in the suite — no real Ollama or GPU needed for tests.

### If you change an API route

Regenerate the frontend's types and commit both files:

```bash
gramvault openapi --out frontend/openapi.json
cd frontend && npm run gen:api && cd ..
```

CI's `api-types` job fails if `frontend/openapi.json` / `frontend/src/api/schema.d.ts` drift from the routes.

### If you change the DB schema

Add a numbered migration under `backend/gramvault/db/migrations/` **and** keep `backend/gramvault/db/schema.sql` in sync by hand — a parity test in `test_db_migrations.py` enforces it. See `docs/DESIGN.md` §A1.

## Code style

- **Python**: type hints on public functions; clean under `ruff check .` (rule set in `pyproject.toml`). No new warnings, no unused imports, no bare `except`.
- **TypeScript**: strict mode; avoid `any`, prefer explicit types on exported components/functions. `tsc -b` and `oxlint` must be clean. New code should pull request/response types from `frontend/src/api/schema.ts` (generated) rather than adding to the hand-written `types.ts`.
- Keep modules focused and well-documented — this codebase favours small modules with a module docstring over large ones.
- Match the patterns already in the file/directory you're editing.

## Submitting a pull request

1. Fork and branch off `main` (`git checkout -b feature/short-description`).
2. Make your change, with tests for new behaviour and updated tests for changed behaviour.
3. Run the full check locally (see above). All green before you open a PR.
4. Write a clear PR description: what changed and *why*.
5. Keep PRs scoped to one change.

For a larger change (a new page, a new pipeline stage, a new export format), open an issue first — and check `docs/DESIGN.md` for the rationale behind how things are currently built.
