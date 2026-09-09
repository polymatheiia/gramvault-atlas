# GramVault Atlas — design notes

This is the design document and running changelog for the **GramVault
Atlas** fork: turning an ad-hoc personal reels-triage workflow
(pull → enrich → OCR → categorize → digest) into first-class GUI features
on top of upstream GramVault, plus category filtering, an expanded
Obsidian exporter, provider abstraction, and packaging.

It was written against the actual code (`backend/gramvault/**`,
`frontend/src/**`, `Dockerfile`, `docker-compose.yml`), and its
**"## Progress"** section below records what each fork commit did. Section
numbers (`§A1`, `§H2`, …) are referenced from code comments.

Architecture recap (what upstream provides):

Architecture recap (what exists):
- FastAPI, plain `sqlite3` (no ORM), routers in `api/routes_*.py`. Schema is a
  single `CREATE TABLE IF NOT EXISTS` script (`db/schema.sql`) that
  `session_scope()` re-runs on **every** connection.
- Jobs: a status column *is* the queue (`items.enrichment_status`,
  `import_jobs`, `export_jobs`); work runs as FastAPI `BackgroundTasks`; the
  frontend polls `/progress`. No Celery/Redis — keep that.
- One LLM path, `ai/ollama_client.py` (httpx → Ollama native API), imported
  directly by `ai/pipeline.py`, `chat/retrieval.py`, `chat/service.py`,
  `api/routes_enrich.py`, `api/routes_chat.py`, `cli.py`.
- Chat already has a citation convention: the model emits `[[item:<id>]]`,
  `chat/service.py::parse_citations` extracts them, `lib/citations.tsx` renders
  chips. SSE streaming for POST is hand-parsed in `api/client.ts`.
- Obsidian export: one note per item (`export/markdown_builder.py`), idempotent on
  `gramvault_id` frontmatter, plus a Dataview index. Re-export **overwrites the
  whole note**.
- Full test suite at `backend/tests/` (293 tests). No auth of any kind on the API.

---

## Progress

What each fork commit added, in build order (§J). Commit hashes are from
this repository's history.

- **Step 1 — done** (`23086f8`). `db/migrations/` + `001_jobs.sql`,
  `PRAGMA user_version` runner in `db/session.py`, WAL/busy_timeout pragmas,
  `jobs` table (partial UNIQUE index = DB-level single-flight), `api/jobs.py` +
  `api/routes_jobs.py`, startup orphan-reclaim in `main.py`, `gramvault migrate`
  CLI, `routes_enrich` through the jobs table (409 on concurrent run,
  cancellable). +30 tests.
- **Step 2 — done** (`bc3a571` backend, `826e679` frontend). Migration 002:
  `categories` table + `items.category_*` columns; `DEFAULT_CATEGORIES` (the 14 +
  descriptions) as the single seed source. `GET /api/library/items?category=`,
  `GET /api/library/categories` (with counts), `POST/PATCH/DELETE
  /api/library/categories`, `PATCH /api/library/items/{id}` for manual
  assignment. `gramvault import-categories <csv>`. Gallery category dropdown +
  ItemCard chip + ItemDetail picker. +18 tests (334 total), ruff + tsc + oxlint
  clean. Live 1814-item DB migrated and backfilled (distribution unchanged from
  the reels-workflow run).

- **Step 3 — done.** Provider abstraction + Models/secrets UI + optional auth.
  - **3a done** (`39a1497`): `ai/providers/` — `Provider` protocol +
    Ollama / OpenAI-compat / Anthropic adapters (httpx only), `ai/errors.py`,
    config `ai:` / `providers:` / `auth:` blocks with legacy-`models:` fallback,
    `secrets.yaml` (gitignored) deep-merged at load, `ollama_client` gains
    list/pull/delete. **No call sites switched** — behaviour unchanged. +13 tests
    (347 total).
  - **3b done** (`36e8558`): all six call sites now go through
    `get_provider(task)` — `ai.chat` / `ai.vision` / `ai.embedding` in config
    take effect. Default Ollama config behaves identically. `chat.retrieval`
    also resolves category so search results carry it. +1 test (348 total).
    Verified live: semantic search runs through the OllamaProvider.
  - **3c done** (`7917851`): `routes_models.py` — `GET /api/models` (+
    `/suggested`), `POST /pull` + `DELETE` (Ollama, via `model_pull` job),
    `PUT /tasks` (persists to `secrets.yaml` via `config.update_secrets`,
    flags `needs_reembed`), `PUT /secrets` (write-only), `POST /test`,
    `POST /reembed` job (`embedding_store.reset_collection`). +12 tests
    (360 total). Verified live against real Ollama.
  - **3d done** (`d21b0cb`): Settings → "AI models & providers" panel — per-task
    provider/model editor with inline Test, provider key fields, Ollama
    pull/remove with live progress + suggested list, re-embed banner, auth-token
    field. `api.put` added.
  - **3e done** (`a8f9b68`): `BearerAuthMiddleware` (pure-ASGI, SSE-safe) on
    `/api/*` + `/media/*`, `/api/health` open, token read live from config so no
    restart needed; frontend sends `localStorage.gv_token` on every request and
    `<AuthGate>` prompts on 401. +6 tests (365 total). Verified live.

- **Step 5 — done.** §C enrichment & OCR in the GUI.
  - **backend** (`2703a0c`): migration 003 — `media_files.ocr_text` /
    `ocr_attempted_at` / `ocr_model` + `vision_model` / `transcript_model`
    provenance. `ocr_attempted_at` stamped even on a discarded read, so the
    `IS NULL` queue stops re-running Cyrillic frames; `retry_discarded` scope
    re-runs `ocr_text IS NULL AND ocr_attempted_at IS NOT NULL`.
    `pipeline.process_item(steps=, ocr_scope=)` — four independent resumable
    passes; OCR reads photos/slides directly, samples a video frame ~2 s in,
    runs `ocr.clean_output`, and leaves `enrichment_status` alone unless
    `embed` is on. `document_builder` emits `on-screen text:` separately;
    keyword search indexes it. `POST /api/enrich/run` gains
    `{scope, steps, ocr_scope}` (legacy `{item_ids}` still works);
    `GET /api/enrich/progress` gains per-pass `done/pending` + `job_id`;
    new `GET /api/enrich/failures`. `gramvault ocr` writes `ocr_text`.
    +13 tests (378 total).
  - **frontend** (`aacbfb8`): new **Enrich** page/route — four pass toggles
    with live "N need this" counts, OCR scope selector, vision model shown
    inline with a link to Settings, whole-library / by-category scope +
    only-missing, Run/Cancel, four progress bars, a failures list with
    per-item Retry.

- **Step 4 — done.** B3 classifier + review queue + Categorize page + eval harness.
  - `ai/classifier_keywords.py` — the `KEYWORDS` / `TAG_OVERRIDE` table
    ported verbatim from the reels-workflow `classify.py`.
  - `ai/classifier.py` — `keyword_vote()` (deterministic multilingual
    vote → `CategoryResult{category, confidence, reason, source}`,
    `needs_review` = confidence < 0.6, mirroring classify.py's
    `score < 3 or margin < 1.5`), `llm_classify()` (token-budgeted
    ~6k-token batches through the `categorize` provider → strict JSON,
    one retry then per-batch fallback to the keyword guess), and
    `categorize_items()` orchestrating `keyword` / `llm` /
    `keyword_then_llm` and writing `items.category_*`. A `manual` label
    is never overwritten.
  - `api/routes_categorize.py` — `POST /api/categorize/run`
    `{scope: uncategorized|needs_review|all|{item_ids}, method}` →
    `jobs.kind='categorize'` (409 on conflict, cancellable, 503 when the
    LLM provider isn't ready); `GET /api/categorize/progress`.
  - Review queue: `GET /api/library/items?needs_review=1`; `Item` gains
    `category_reason`.
  - `gramvault categorize --method --scope` CLI for offline / cron use.
  - +24 tests (404 total), ruff clean.
  - **eval harness** (`3f7038f`): `backend/tests/eval/` — `loader.py` joins
    `categories_final.csv` (gold) to `items_raw.json` (enriched text) into
    `Item` objects; `metrics.py` = per-category precision/recall/F1 +
    confusion; `test_classifier_eval.py` scores the keyword vote and (behind
    `GRAMVAULT_EVAL_LLM=1`) the LLM pass, and checks low-confidence guesses
    are the wrong ones. Dataset is real personal data → gitignored
    (`backend/tests/eval/data/`) / env-var paths, tests skip when absent.
    `eval` marker filtered out of the default run (`addopts` in pyproject),
    so CI is untouched. Local baseline: keyword vote **61.7%** acc / 0.61
    macro-F1; confident guesses 81%, needs-review 40%.
  - **frontend**: new **Categorize** page/route — method
    picker (keyword / keyword-then-llm / llm) with the model shown inline,
    scope picker (uncategorized / needs_review / all), Run/Cancel with a
    live job progress bar, a stat strip (total/categorized/uncategorized/
    needs-review) + by-source line, and the review-queue grid
    (`?needs_review=1`) with a per-card category dropdown that PATCHes to a
    manual label and drops the card. Nav: `… Enrich · Categorize · Settings`.

- **Step 7 (§D digests) — done.**
  - **backend** (`870452e`): migration 004 `digests` table (one row per
    run, never overwritten; item-id snapshot + provider/model/tokens/cost
    manifest). `ai/digest.py` — `select_items` (ids / category / semantic
    query / query∩category), token-budgeted `plan_batches`,
    `_extract_batch` (strict JSON array, one retry, drops out-of-selection
    citations), `_reduce` with a two-level fallback (`_chunk_rows` →
    fragments → merge) when the entries exceed budget, `_postprocess`
    (pure code: strips dangling `[[item:<id>]]`, rewrites each
    singly-cited line's link + @handle from the DB), `run_digest` (job
    body), `preflight` (estimate). `ai/digest_templates/*.yaml`:
    book-titles, advice-digest, link-list, media-titles, linked-findings
    (+ user overrides in `<data>/digest_templates/`).
    `api/routes_digests.py`: templates / preflight / create (202,
    `jobs.kind='digest'`, 409, 503) / history / get / download.
    `gramvault digest` CLI. Extract batch budget is 3000 tokens so a
    default local Ollama (`num_ctx` 4096) doesn't thrash — but see §I:
    a real digest still wants a hosted `digest` provider. +21 tests.
  - **frontend** (`53d5580`): `/digest` page — template picker (+ prompts),
    category/query selection, Estimate (preflight), Generate with a polled
    progress bar, Markdown preview + client-side `.md` download, history.

- **Step 8 (§G Obsidian) — done** (G1–G6 + layout UI).
  - **G1 managed region** (`6ef07b0`): a note is frontmatter +
    `%% gramvault:start %%` … body … `%% gramvault:end %%` + the user's
    own content. Re-export regenerates only GramVault's frontmatter keys
    and the managed body; extra frontmatter keys and everything after the
    end marker survive. Legacy marker-less notes become managed once.
  - **G2 frontmatter + layout** (`6ef07b0`): frontmatter gains `account`,
    `saved_at`, `category`, `category_source`, `enrichment{...}`, splits
    `#hashtags` from `tags`. `export.layout` = `flat` | `by-category` |
    `by-date`; the recursive stale-note scan moves notes (with their user
    tail) when layout/category changes. Body gains `## On-screen text`.
  - **G5 dashboard** (`123dc1b`): `GramVault Dashboard.md` — per-category
    counts, uncategorized/low-confidence/failed metrics, last 30 days.
  - **G4 digest export** (`123dc1b`): `POST /api/digests/{id}/export` +
    `exporter.export_digest` → `<subfolder>/_digests/<name>.md`, managed,
    `[[item:<id>]]` → `[[<item note>]]`. "Export to Obsidian" button on
    the Digest page.
  - **layout UI** (`57eaaa7`): `GET`/`PUT /api/export/settings` + a layout
    selector in Settings → Obsidian.
  - **G3 category MOCs** (2026-09-09): `export/moc_builder.py` —
    `build_moc_markdown()` renders `<subfolder>/_moc/<category>.md` (managed
    region, user tail preserved): the latest category-scoped digest
    (`[[item:<id>]]` → note links) + a Dataview block (`WHERE category =
    "<x>"`, layout-independent) + a plain-table fallback of the category's
    items. `export_items(..., category_digests=)` writes one MOC per
    category present in the export; `repository.load_latest_category_digests()`
    resolves the dict (newest `done` digest per category, skipping
    query-scoped ones); `routes_export._run_export_job` passes it.
    `ExportResult.moc_paths`. `_escape_table_cell` → public
    `escape_table_cell`. +19 tests.
  - **G6 video poster frames** (2026-09-09): `export/poster.py` —
    `poster_frame()` shells `ffmpeg -ss 1 -frames:v 1` (best-effort, retries
    from frame 0 for a sub-second clip, no-ops without ffmpeg). In `copy`
    mode a video media file also gets `<id>_<seq>.poster.jpg` in `media/`
    (idempotent — skipped when newer than the source); the note embeds the
    still above a `[▶ video](…)` link instead of an inline `![[…mp4]]`.
    `MediaLink` gains `poster`. Also fixed `_copy_or_link_media` to resolve
    library-relative `file_path` rows against `resolved_library_dir` (copy
    mode was silently failing for them). +12 tests.

- **Step 9 (§F Instagram pull) — done (uncommitted). Verified end-to-end
  against real Instagram 2026-09-09** (paste-cookie connect with
  `sessionid`/`ds_user_id`/`csrftoken` grabbed from DevTools → Network →
  an instagram.com request → Cookies; walk + download + import + link all
  worked).
  - New optional dep: `pip install -e ".[instagram]"` → `instaloader>=4.14`.
    `pull:` config block (`enabled: false` default, `session_dir`,
    `max_default`). No DB migration — `jobs.kind='pull'` and
    `items.external_id` already existed.
  - `ingestion/instagram.py`: `_load_instaloader()` (lazy, friendly error),
    `parse_cookies()` (JSON obj / Cookie-Editor array / Netscape — ported
    from `~/reels-workflow/import_session.py`), `read_firefox_cookies()`
    (best-effort local `cookies.sqlite` scan; same-machine only),
    `connect_from_cookies()` / `connect_from_local_browser()` →
    `test_login()` → caches `session-<user>` (chmod 600) + a
    `gramvault-pull.json` sidecar, never the DB. `pull_saved()` walks
    `Profile.own_profile(...).get_saved_posts()` newest-first, stop-on-known
    (`stop_after_known`, or `download_all` to ignore), `max_count` cap,
    cancel between posts, 3–7s jitter; downloads to a temp dir then reuses
    `import_zip` + `link_local_media(copy=True)` **unchanged** (entries are
    built in the `label_values` export shape). Returns
    `{scanned,new,downloaded,imported,linked,failed,stopped_reason,new_item_ids}`.
  - `api/routes_pull.py`: `GET /api/pull/session`, `POST /connect` (paste,
    400 on bad cookies), `POST /connect-local` (Firefox, 404 if none),
    `DELETE /session`, `POST /run` (`jobs.kind='pull'` via
    `asyncio.to_thread`; 403 disabled / 409 conflict / 503 not connected),
    `GET /progress` (mirrors categorize; surfaces the finished/cancelled
    job's result dict). `gramvault pull [--cookies F] [--max N] [--full]`
    CLI for cron.
  - **frontend**: new **Pull** page/route (nav: `… Import · Pull · Enrich
    …`) — disabled-state explainer, Connect (login link + Firefox button +
    collapsible paste box), connected panel (@user, disconnect), walk-back
    count + "pull everything" + Run/Cancel, a 6-stat strip, and
    Enrich/Categorize links for the new items.
  - +34 tests (`test_ingestion_instagram.py` with a fake instaloader,
    `test_routes_pull.py`). ruff + tsc + oxlint clean. README §"Pulling
    your saved posts" + config.example.yaml + privacy-section wording.
  - Requires `pip install -e ".[instagram]"` (instaloader) in the deploy venv.

- **Step 10 (§H2 docker-compose) — done.**
  - `Dockerfile` rewritten multi-stage: `node:22-alpine` builds the SPA →
    `python:3.12-slim` + ffmpeg, `pip install -e ".[instagram]"`, built
    `frontend/dist` copied in, non-root `app` user (uid 1000),
    `HF_HOME=/data/hf`, `GRAMVAULT_CONFIG_PATH=/app/config.yaml`.
    `CMD ["gramvault", "serve"]` (host/port from config).
  - `docker-compose.yml` rewritten: one `gramvault` service,
    `network_mode: host` (Tailscale-IP bind happens in-app, survives boot
    ordering — W2), `restart: unless-stopped`,
    `user: "${GRAMVAULT_UID:-1000}:${GRAMVAULT_GID:-1000}"`.
    `GRAMVAULT_PATHS__{DB_PATH=/data/gramvault.db,CHROMA_DIR=/data/chroma,
    LIBRARY_DIR=/library}` env (so one config.yaml works in and out of the
    container; media rows are stored relative to `library_dir`). Bind
    mounts: `config.yaml` + `secrets.yaml` (rw — Settings persists to
    them), `data/` (db/chroma/keyframes/HF cache), `library/`,
    `~/.config/instaloader`, commented vault mount. Ollama reached at
    `127.0.0.1:11434` via host net; containerized `ollama` service
    commented. Bridge-network alternative documented in the header.
  - `.dockerignore` added (keeps `config.yaml`/`secrets.yaml`/`data/`/
    `library/`/`.venv`/tests out of the build context and image).
  - README gains a "Run it with Docker" subsection.
  - Verified end to end against a full ~1800-item library
    (`.env` with UID/GID, empty `secrets.yaml`, `docker compose up -d
    --build`); `GET /media/<path>` serves from the mounted library,
    migrations run on startup, the process runs as the host uid.

- **Step 10b (§H2 heavy-job semaphore) — done.**
  - Migration `005_heavy_job_lock.py` + `schema.sql`: a second partial
    UNIQUE index `idx_jobs_one_active_heavy ON jobs((1)) WHERE kind IN
    ('enrich','categorize','digest','reembed') AND status IN
    ('pending','running')` — DB-enforced "at most one model-bound job",
    same mechanism as the per-kind index, race-proof. `pull` / `model_pull`
    are network-bound and still overlap freely.
  - `jobs._HEAVY_KINDS` (keep in sync with the index), `jobs.active_heavy()`,
    `jobs._conflict_reason()` — `create()` now raises `JobConflict` with a
    message naming the blocking job's kind. The heavy-kind routes now pass
    `str(exc)` through as the 409 detail (frontend already surfaces
    `err.detail` / `err.message`).
  - +7 tests; a few existing tests that spun up ENRICH+DIGEST together
    repaired to pair a heavy kind with `pull`. 484 pass, ruff/tsc/oxlint
    clean.

- **Step 11 (§A6 openapi type generation) — done.**
  - `gramvault openapi [--out F]` CLI — dumps `create_app().openapi()` as
    key-sorted JSON (stable to diff).
  - `frontend/openapi.json` (committed) + `npm run gen:api`
    (`npx openapi-typescript@7.13.0 openapi.json -o src/api/schema.d.ts`,
    committed, 3.6k lines). Not wired into `npm run build` — the build
    stays hermetic; regen is a dev/CI step.
  - CI `api-types` job: regenerates both and `git diff --exit-code`, so a
    route change without a regen fails CI.
  - `types.ts` header now points new code at
    `components['schemas'][...]` from `schema.d.ts`; the hand-written file
    is retired module-by-module from here (not done wholesale — plan says
    "gradually").
  - +2 CLI tests. 506 pass.

Everything in the build order (§J 1–10 + A6) is done.

**Polish pass (2026-09-09):**
- **G3 stale-MOC cleanup** (`9d693a4`): a whole-library export prunes
  `_moc/<category>.md` for categories with no items left and no user
  notes; partial exports never prune.
- **§B2 FTS5** (this commit): migration 006 `items_fts` (fts5 over
  caption/author/tags/transcript/vision/ocr, `unicode61
  remove_diacritics 2`). `chat/fts.py` (`reindex`, `ensure_populated`,
  `fts_available`) — not trigger-maintained; import/pull + enrichment +
  the tag-edit route call `reindex()` for touched items, and
  `keyword_search` self-heals with a full rebuild if it finds the index
  empty. `keyword_search` now runs an FTS `MATCH` (prefix-AND of query
  tokens, bm25-ranked) and keeps the old LIKE scan as a fallback.
  `gramvault reindex-search` CLI.

**Video scrolling (`f0a33ac`):** a `/feed` reels-style scroll-snap view
(autoplay muted video, carousel strip) plus `←`/`→` prev-next on the item
page, both over any gallery filter. Backed by `GET /api/library/item-ids`
and `GET /api/library/items?ids=`.

Still open (genuinely optional): finishing the `types.ts` → generated
schema migration module by module; the A6 single-page `/pipeline` nav
(superseded by the separate pages, which work).

---

## 0. Review of v1 — weaknesses and what changes

### 0.1  Foundations v1 skipped entirely

| # | Weakness | Evidence | Fix (→ section) |
|---|---|---|---|
| F1 | **No migration mechanism.** v1 said "`ALTER TABLE items ADD COLUMN category`" as if it were trivial. `schema.sql` is `CREATE TABLE IF NOT EXISTS` only — appending a column there does nothing for an existing DB. | `db/schema.sql` header; `db/session.py::init_db` | Add `PRAGMA user_version` + `db/migrations/NNN_*.sql`, run once at startup, stop running `init_db` per connection. → §A1 |
| F2 | **SQLite will lock under concurrent jobs.** `get_connection()` sets no `journal_mode=WAL`, no `busy_timeout`. Enrich + categorize + a gallery page load = `database is locked`. | `db/session.py::get_connection` | WAL + `busy_timeout=5000` + one writer-job at a time. → §A2 |
| F3 | **Crashed jobs are orphaned forever.** `resolve_target_item_ids` re-queues only `pending`; a restart mid-run leaves items at `running`, which nothing ever picks up again (the pipeline docstring claims otherwise). | `ai/pipeline.py::resolve_target_item_ids` | Startup hook: `UPDATE items SET enrichment_status='pending' WHERE enrichment_status='running'`; same for every new job table. → §A2 |
| F4 | **No single-flight guard.** Two `POST /api/enrich/run` calls both schedule `process_items` over the same ids. Adding four more job types multiplies this. | `api/routes_enrich.py` | A `jobs` table + an in-process `asyncio.Lock` per job kind; `409 Conflict` if one is running. → §A2 |
| F5 | **No auth, and v1 proposed adding API keys + an Instagram session to that API.** Anyone who can reach the port can read secrets, trigger pulls, and wipe categories. Tailnet-only binding is the only protection today. | route listing; `main.py` | Optional bearer token (`server.auth_token`), enforced by middleware when set; secrets endpoints are write-only (never echo keys); document that `0.0.0.0` is unsafe. → §A3 |
| F6 | **v1 claimed "no test harness" — wrong.** There is a full suite at `backend/tests/` (293 tests, `conftest.py` with `tmp_config`/`tmp_db_conn`/`client` fixtures, `pyproject.toml` pytest config); the repo-root `tests/` I looked at only holds fixtures. So A4 is *extend*, not *build*: add migration/jobs tests to the suite, and add `~/reels-workflow/categories_final.csv` (1814 hand-labelled items) as a **classifier eval set**. → §A4 |

### 0.2  Designs v1 left underspecified

| # | Weakness | Fix (→ section) |
|---|---|---|
| U1 | **Digests were "batch → concatenate".** That yields five disjoint half-documents with duplicate headings. The by-hand process was map → *reduce*: per-batch structured extraction, then one merge/dedupe/render pass. Batches must be sized by **tokens**, not item count (106 research items ≈ 190 KB ≈ 50k tokens). | Explicit map/reduce with JSON intermediates; token-budgeted batching; `[[item:<id>]]` citations reused from chat. → §D |
| U2 | **OCR "discard" is invisible.** `ocr.clean_output` returns `None` for Cyrillic/refusals → `vision_caption` stays NULL → the queue (`vision_caption IS NULL`) re-runs the same frame forever. v1 said "column *or* provenance" — pick one. | Separate `ocr_text` + `ocr_attempted_at` + `ocr_model` columns; the document builder labels OCR distinctly from scene captions. → §C |
| U3 | **Category taxonomy as a config list dangles on rename.** Rename `home ideas` → `home` in config and 26 rows silently point at nothing. | `categories` table (seeded from config), rename/merge endpoints that cascade. → §B1 |
| U4 | **"Local 3B does the rough categorize pass" was overstated.** The keyword vote is the free rough pass; the LLM pass is where accuracy comes from, and a 3B model won't beat the keyword vote by much on this multilingual corpus. | Recommend: keyword pass (free) → API model for the LLM pass (~$0.50 for 1814 items on Haiku) → local model only as an offline fallback. Measure against the eval set. → §B3, §I |
| U5 | **Category during semantic search** — v1 offered two options. | Post-filter in SQL after Chroma returns candidates (always correct, no metadata sync on manual edits). Don't put `category` in Chroma metadata. → §B2 |
| U6 | **Provider abstraction ignored `ensure_model_pulled`.** API providers have no "pull"; the Protocol needs `ensure_ready()` semantics that mean different things per provider. Also embeddings-via-API changes dimension *and* costs money per re-embed. | Protocol with `ensure_ready()`; embeddings stay local by default with a loud warning if switched. → §A5 |
| U7 | **Digest → Obsidian wikilinks by matching `@account (reel)` text** is fragile. | Have the digest LLM emit `[[item:<id>]]`; the exporter rewrites to `[[<note filename>]]`. Same regex the chat already uses. → §D, §G |
| U8 | **Navigation** — v1 hedged between separate pages and one Pipeline page. | One **Pipeline** page with ordered tabs (Pull → Enrich → Categorize → Digest) and a shared job-status strip. → §A6 |
| U9 | **Pull job design ignored the event loop.** Instaloader is synchronous and slow; scheduling it as an `async` background task would block every other request. | `asyncio.to_thread` per post, cancellable, progress written to SQLite. → §F |
| U10 | **`types.ts` is hand-synced** — v1 adds ~20 endpoints. | Generate types from `/openapi.json` (`openapi-typescript`) as a build step. → §A6 |

### 0.3  Recommendations that were wrong or risky

| # | Weakness | Fix |
|---|---|---|
| W1 | **systemd unit had `After=docker.service`** — a *user* unit can't order after a *system* unit. Also no `Environment=`. | Corrected unit in §H1. The app starts fine without Ollama (health is lazy), so no ordering is needed. |
| W2 | **`ports: ["100.x.y.z:8777:8000"]` in compose can fail at boot** — Docker may start before `tailscale0` has the address ("cannot assign requested address") and the container then never binds. | Use `network_mode: host` + in-app `--host 100.x.y.z` (and `Restart=always` handles the race), or a tailscale sidecar. → §H2 |
| W3 | **`init_db()` on every request** — cheap today, but with migrations it becomes wrong (migrations must run once). | Move to a startup event. → §A1 |
| W4 | **"Vendor instaloader into the repo"** — it's on PyPI; vendoring was only because `~/save-insta-posts` did it. | Optional extra `pip install -e ".[instagram]"`. → §F |
| W5 | **Deps**: v1 implied SDKs for OpenAI/Anthropic. Every provider is a JSON HTTP API; `httpx` (already a dep) is enough and keeps the footprint small, matching `ollama_client.py`. | Raw httpx clients. → §A5 |

### 0.4  Concerns v1 didn't raise at all

- **Cost & time budget** for the API-backed steps — the user needs numbers to decide local vs cloud. → §I
- **Reproducibility** of digests: which items, which template version, which model. Store the input manifest with the output. → §D
- **Frontend `mediaUrl` comment is stale** ("GAP: no /media mount") — the mount exists in `main.py`. Housekeeping.
- **FTS5** — `chat/retrieval.py::keyword_search` is a `GROUP_CONCAT` + `LIKE` over the whole table per query. Fine at 1.8k items; "digest by search" and category-scoped search make an `items_fts` table worth adding. → §B2 (optional)
- **Per-item audit trail** for category ("why did it land here?") — needed for the review queue to be usable. → §B1

---

## A. Foundations (do these before any feature)

### A1  Migrations
- `db/migrations/001_category.sql`, `002_ocr.sql`, `003_jobs.sql`, … — plain SQL,
  applied in order when `PRAGMA user_version < N`, inside one transaction each.
- `db/session.py`: new `migrate(conn)`; `init_db` runs `schema.sql` **then**
  `migrate`. Call it **once** from a FastAPI `lifespan` startup hook and from the
  CLI entry, not from `session_scope()`.
- `schema.sql` stays the "fresh DB" definition and is kept in sync by hand with
  the migrations (state this rule in the file header).
- Rule for all new per-item state: **real columns, not `raw_metadata_json`**
  (the pipeline already abuses that column for `enrichment_error`; don't extend
  the habit).

### A2  Concurrency & job hygiene
- `get_connection()`: `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
  PRAGMA synchronous=NORMAL`.
- New table:
  ```sql
  CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('import','pull','enrich','categorize','digest','export','model_pull','reembed')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed','cancelled')),
    params_json TEXT, progress_json TEXT, result_json TEXT, error_message TEXT,
    started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now'))
  );
  ```
  Existing `import_jobs`/`export_jobs` can stay; new job kinds use `jobs`.
- `api/jobs.py`: `run_job(kind, params, coro)` — acquires a per-kind
  `asyncio.Lock` (409 if held), inserts the row, runs the coroutine via
  `BackgroundTasks`, updates `progress_json` as it goes, sets `done/failed`.
  Supports `cancel` via an `asyncio.Event` checked between items.
- **Startup reset**: `running` → `pending` on `items.enrichment_status` and
  `jobs.status` (with `error_message='interrupted by restart'`).
- `GET /api/jobs?kind=&status=` and `GET /api/jobs/{id}` — one polling surface
  for the whole Pipeline page.

### A3  Minimal auth
- `server.auth_token: null` in config. When set, a middleware requires
  `Authorization: Bearer <token>` on `/api/*` (not on `/media`? — no: on `/media`
  too, it's your library). The SPA stores the token in `localStorage` after a
  one-time prompt.
- Secrets endpoints are **write-only** — `GET /api/models/config` returns
  `api_key_set: true`, never the key.
- README/Settings: "bind to `127.0.0.1` or your Tailscale IP; never `0.0.0.0`
  without `auth_token`."

### A4  Test harness — *extend* the existing suite
`backend/tests/` already exists (293 tests, `conftest.py` fixtures). Add:
- `test_db_migrations.py` — version tracking, fresh-vs-existing paths, schema.sql
  ↔ migration parity (comment-stripped normalisation), WAL pragma. **Done in
  step 1.**
- `test_jobs.py` / `test_routes_jobs.py` — job lifecycle, conflict, cancel,
  orphan reclaim. **Done in step 1.**
- Later: unit tests for `ai/classifier`, `export/markdown_builder` managed-region
  merge, digest map/reduce.
- **Classifier eval**: `backend/tests/eval/categories_final.csv` (copied from
  `~/reels-workflow`) + `items_raw.json` → `pytest -m eval` prints
  accuracy/confusion for the keyword pass and (with a key) the LLM pass. 1814
  human-labelled multilingual items — the single most valuable test asset.
- CI (`.github/workflows/ci.yml`): already runs `ruff` + `pytest`; add the
  `eval` marker to the default skip list.

### A5  Provider abstraction
`backend/gramvault/ai/providers/`:

```python
class Provider(Protocol):
    name: str
    async def ensure_ready(self, model: str) -> None: ...   # ollama: pull if missing; api: check key present
    async def chat(self, model, messages, *, temperature=0.2, json_mode=False, max_tokens=None) -> str: ...
    async def stream_chat(self, model, messages, **kw) -> AsyncIterator[str]: ...
    async def embed(self, model, text) -> list[float]: ...
    async def caption_image(self, model, image_path: Path, prompt: str) -> str: ...
```
- `ollama.py` — thin wrapper over today's `ollama_client.py` (keep that file;
  make it the implementation).
- `openai_compat.py` — `base_url` + key; covers OpenAI, OpenRouter, Groq,
  Together, DeepInfra, and local llama.cpp/LM Studio/vLLM (and Ollama's `/v1`).
  `caption_image` = image as base64 `image_url` content part.
- `anthropic.py` — Messages API; `caption_image` = base64 image block.
- `registry.get_provider(task) -> (Provider, model)` from config:
  ```yaml
  ai:
    chat:       {provider: ollama,    model: llama3.2:3b}
    vision:     {provider: ollama,    model: minicpm-v}     # or anthropic/claude-sonnet-5 for real OCR
    embedding:  {provider: ollama,    model: bge-m3}        # keep local — see warning
    categorize: {provider: anthropic, model: claude-haiku-4-5}
    digest:     {provider: anthropic, model: claude-sonnet-5}
  providers:
    ollama:    {host: http://localhost:11434}
    anthropic: {api_key_env: ANTHROPIC_API_KEY}
    openai:    {api_key_env: OPENAI_API_KEY, base_url: null}
    openrouter:{api_key_env: OPENROUTER_API_KEY, base_url: https://openrouter.ai/api/v1, kind: openai_compat}
  ```
  Back-compat shim: if `models:` is present and `ai:` absent, map it.
- **Secrets**: `secrets.yaml` beside `config.yaml` (`chmod 600`, in
  `.gitignore`), merged over `providers.*.api_key`; env var wins over file.
- Call-site migration (six modules) is mechanical: `ollama_client.embed(...)` →
  `get_provider("embedding")`. Transcription stays `faster-whisper` (not a
  provider — it's a local library, and Whisper-via-API is a separate later
  option).
- **Embedding warning**: switching the embedding provider/model changes the
  vector dimension → Chroma collection must be dropped and every item
  re-embedded (`jobs.kind='reembed'`). If the new provider is an API, that's
  ~1814 × ~500 tokens ≈ 1M tokens per re-embed. The Models page must show this
  before applying. Default advice in the UI: keep `bge-m3` local.

### A6  Frontend scaffolding
- `openapi-typescript` → `src/api/schema.d.ts` from `/openapi.json` in
  `npm run build`; retire hand-edited `types.ts` gradually.
- Generalise `streamChatMessage` into `streamSSE(path, body, handlers)` — the
  model-pull and digest endpoints stream too.
- New route `/pipeline` with tabs `Pull · Enrich · Categorize · Digest`, a
  sticky **job strip** at the top (`GET /api/jobs?status=running|pending`),
  and a per-tab history list. Nav becomes `Gallery · Chat · Pipeline · Settings`.
- `Settings` gains tabs `Obsidian · Models · Security`.

---

## B. Categories

### B1  Schema
```sql
-- 001_category.sql
CREATE TABLE categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  color TEXT,                 -- optional UI hint
  description TEXT            -- shown to the LLM classifier as the definition
);
ALTER TABLE items ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE items ADD COLUMN category_source TEXT CHECK (category_source IN ('keyword','llm','manual'));
ALTER TABLE items ADD COLUMN category_confidence REAL;   -- 0..1; keyword pass maps margin→conf
ALTER TABLE items ADD COLUMN category_reason TEXT;       -- keyword hits or one-line LLM rationale
ALTER TABLE items ADD COLUMN category_updated_at TEXT;
CREATE INDEX idx_items_category ON items(category_id);
```
- Seed `categories` from `config.categorize.categories` on first migration
  (the 14, with short descriptions — the descriptions matter: they become the
  classifier's rubric).
- Endpoints: `GET/POST/PATCH/DELETE /api/library/categories` (rename cascades
  by id; delete requires a `move_to` target); `PATCH
  /api/library/items/{id}` with `{category_id}` → `source='manual'`,
  `confidence=1`, and **manual is never overwritten** by a re-run.
- Backfill: a one-off `gramvault import-categories ~/reels-workflow/categories_final.csv`
  (source=`manual`, confidence=1) so the 1814 existing labels land immediately.

### B2  Filter
- `routes_library.list_items`: `category: str | None` (`__none__` → `IS NULL`);
  `GET /api/library/categories` returns `[{id,name,count}]`.
- `Gallery.tsx`: `<select>` with counts, "Uncategorized" option; `ItemCard`
  chip; `ItemDetail` dropdown (PATCH).
- **Semantic search + category**: `GET /api/chat/search?q=&category=`: run
  `hybrid_search` with `top_k*3`, then `WHERE id IN (...) AND category_id=?` in
  SQL, truncate to `top_k`. Keep the filter enabled during search in the UI.
- Optional: `items_fts` (FTS5 over caption+transcript+ocr+vision) replacing the
  `LIKE`/`GROUP_CONCAT` keyword pass — needed once "digest by search" exists.

### B3  Classifier
`ai/classifier.py` (port of `~/reels-workflow/classify.py`):
- `keyword_vote(item, categories) -> KeywordResult(category, score, margin,
  hits: dict)`; confidence = `sigmoid(margin)`-ish, `needs_review = score < 3
  or margin < 1.5` (the thresholds that worked). `KW` table lives in
  `ai/classifier_keywords.py`, editable; per-category `description` from the
  table is appended for the LLM.
- `llm_classify(items, provider, model)`: batches of ≤ ~6k tokens (measure with
  a cheap tokenizer estimate: `len(text)//4`), prompt = rubric (category names
  + descriptions) + the D3 line format
  `#id [type] @user | T: | C: | TR: | V: | AUTO=` → strict JSON
  `{"<id>": {"category": "...", "confidence": 0.0-1.0, "reason": "..."}}`,
  `json_mode=True`, retry once on parse failure with a "return only JSON"
  nudge, then fall back to the keyword result for that batch.
- `POST /api/categorize/run` `{scope: uncategorized|needs_review|all|ids,
  method: keyword|llm|keyword_then_llm}` → `jobs.kind='categorize'`.
- **Review queue**: `GET /api/library/items?needs_review=1` (= `source !=
  'manual' AND confidence < 0.6`), rendered as a grid with the reason and a
  one-click dropdown. This replaces the by-hand D3 step.
- Eval (§A4) gates changes to the keyword table.

---

## C. Enrichment & OCR in the GUI

### C1  Schema
```sql
-- 002_ocr.sql
ALTER TABLE media_files ADD COLUMN ocr_text TEXT;
ALTER TABLE media_files ADD COLUMN ocr_attempted_at TEXT;
ALTER TABLE media_files ADD COLUMN ocr_model TEXT;
ALTER TABLE media_files ADD COLUMN vision_model TEXT;       -- provenance for captions too
ALTER TABLE media_files ADD COLUMN transcript_model TEXT;
```
- `ocr_attempted_at` is set even when `clean_output` discards the result — the
  queue becomes `ocr_attempted_at IS NULL` (or `< the model change date`), so
  Cyrillic frames stop being re-run until a stronger vision model is configured,
  at which point a "retry discarded with new model" scope exists.
- `document_builder` emits `on-screen text:` (OCR) separately from
  `visual description:` — retrieval and digests treat them differently.
- Carousels: OCR every slide (`sequence_index` loop), not just frame 2 s of a
  video — this is exactly the "read the slides directly" step that recovered
  ~460 book titles.

### C2  API
`POST /api/enrich/run`:
```json
{"scope": {"item_ids": null, "category": null, "only_missing": true},
 "steps": {"transcribe": true, "ocr": true, "vision_caption": false, "embed": true},
 "ocr_scope": "silent_thin_caption" | "all_silent" | "all_media" | "retry_discarded"}
```
`GET /api/enrich/progress` → per-step counts (`need/done` for each) + the job id.
Keep `POST /api/enrich/run {item_ids}` working as the legacy shape.

### C3  UI (Pipeline → Enrich tab)
Four step toggles with the live "N need this" count next to each, an OCR scope
selector, the vision/OCR model shown inline (from `ai.vision`) with a link to
Models, Run/Cancel, four progress bars, a failures table (`enrichment_error`,
retry button).

---

## D. Digests (the recommendation/notes docs)

### D1  Model: map → reduce, not concatenate
1. **Select**: items by category and/or search and/or ids; pull the *full* text
   (caption, transcript, OCR, vision, tags, author, permalink) — not the
   gallery's truncated caption.
2. **Map**: token-budgeted batches (~8k tokens in, leave room). Per batch the
   template's *extract* prompt returns **JSON**, e.g. for `book-titles`:
   `[{"title","author","kind":"book|manga|series","theme","note","item_id"}]`;
   for `advice-digest`: `[{"theme","technique","detail","item_id"}]`.
3. **Reduce**: one call over the union of extracted entries (or a
   two-level reduce if it exceeds budget): dedupe (fuzzy on title/author, fix
   Whisper mishearings — give the model the alias list we built), group by
   theme, order, and **render Markdown** in the house style:
   `- **X** — detail — @account ([reel](url)) [[item:123]]`.
   Non-English items summarised in English; pure promo dropped; unrecoverable
   items listed in a "Still not recovered" table with links.
4. **Post-process** (code, not LLM): validate every `[[item:<id>]]` exists,
   resolve `@account`/permalink from the DB (never trust the model for URLs),
   strip dangling citations.

### D2  Templates
Shipped in `ai/digest_templates/*.yaml` — `{name, description, extract_prompt,
extract_schema, reduce_prompt, default_task: digest}`; user-editable copies in
`data/digest_templates/`. Initial set mirrors what we produced:
`book-titles`, `media-titles` (film/TV/anime), `site-list`, `advice-digest`
(psychology/lifehacks/beauty), `linked-findings` (research: study, authors,
journal, paper URL if identifiable — *mark unverified*), `cookbook`,
`place-list`, `retriage` (cluster a bucket and report sizes).

### D3  Storage & API
```sql
-- 003_digests.sql
CREATE TABLE digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, template TEXT NOT NULL,
  template_version TEXT, selection_json TEXT NOT NULL,   -- {category, query, item_ids}
  item_ids_json TEXT NOT NULL,                           -- the exact snapshot used
  provider TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_estimate REAL,
  markdown TEXT, status TEXT, job_id INTEGER, created_at TEXT DEFAULT (datetime('now'))
);
```
- `GET /api/digests/templates`, `POST /api/digests` (creates + runs job, SSE
  progress: batch i/N, reduce), `GET /api/digests/{id}` (markdown + manifest),
  `POST /api/digests/{id}/export` (→ Obsidian, §G), `GET …/download`.
- Re-run with the same selection = new row (history/diff), not overwrite.

### D4  UI (Pipeline → Digest tab)
Selection (category / search / current gallery filter), template picker with
the prompts visible and editable, model picker (default `ai.digest`), a
**pre-flight estimate** (items, tokens, ~cost, ~time), Run, streamed progress,
Markdown preview, Export/Download, history list.

---

## E. Models & secrets (Settings → Models)

- `GET /api/models` — Ollama `api/tags` (name, size, modified) + configured
  providers with `api_key_set`.
- `GET /api/models/suggested` — static, tuned for no-GPU/7 GB:

  | task | model | size | note |
  |---|---|---|---|
  | embedding | `bge-m3` | 1.2 GB | multilingual; current; keep |
  | embedding | `nomic-embed-text` | 274 MB | English-only, lighter |
  | vision/OCR | `minicpm-v` | 5.5 GB | Latin only, ~5 min/img here |
  | vision/OCR | `moondream` | 1.7 GB | fast, weak on small text |
  | chat/categorize | `llama3.2:3b` | 2 GB | current |
  | chat/categorize | `qwen2.5:3b` | 1.9 GB | better JSON compliance |
  | chat | `llama3.1:8b` | 4.9 GB | tight; swap-heavy |

- `POST /api/models/pull {name}` → proxies Ollama `api/pull` (streams
  `{status, completed, total}`) as SSE via `streamSSE`; `jobs.kind='model_pull'`.
  `DELETE /api/models/{name}`.
- `PUT /api/models/tasks {task, provider, model}`; `PUT /api/models/secrets
  {provider, api_key}` (write-only; writes `secrets.yaml`, `chmod 600`);
  `POST /api/models/test {task}` — one tiny call to verify the key/model works.
- Embedding change → confirmation modal → `jobs.kind='reembed'`.

---

## F. Instagram pull (opt-in)

- Dependency: `pip install -e ".[instagram]"` → `instaloader>=4.15`.
- `ingestion/instagram.py`: `import_cookies(json) -> username` (accepts dict /
  Cookie-Editor list / Netscape; `test_login()`), `pull_saved(known_ids,
  max, stop_after_known=5, progress_cb, cancel_ev)` — each `download_post` in
  `asyncio.to_thread`, 3–7 s jitter, writes to a temp dir, fixes media type
  from the downloaded files (the A4 step, folded in), returns the
  export-shaped JSON → `importer.run_import` → `linker.link_local_media`.
- Session file at `~/.config/instaloader/session-<user>` (`chmod 600`), path
  from `pull.session_dir`; **never** in the DB; `GET /api/pull/session` returns
  only `{configured, username, last_verified_at}`.
- Config `pull: {enabled: false, session_dir: ~/.config/instaloader, max_default: 400}`.
- UI (Pipeline → Pull tab): disabled state with the ToS/rate-limit note until
  enabled in config; paste/upload cookies → Connect → Pull → progress
  (`{scanned, new, downloaded, imported, linked, stopped_reason}`) → "Enrich
  these N" / "Categorize these N" buttons.
- Positioning: README gets an explicit "Optional: fetching your own saved
  posts with your session" section; default off.

---

## G. Obsidian

### G1  Managed region (fixes the clobber)
`build_note_markdown` emits:
```
---frontmatter---
%% gramvault:start %%
…generated body…
%% gramvault:end %%

<user notes preserved below>
```
`exporter.export_items`: if the note exists, parse frontmatter + replace only
the region between markers (regenerate frontmatter fields GramVault owns; keep
unknown frontmatter keys the user added, e.g. `rating`). Legacy notes without
markers: treat the whole body as managed once, then insert markers.

### G2  Frontmatter & layout
- Add `category`, `category_source`, `account` (alias), `saved_at`
  (`imported_at`), `hashtags` (kind=hashtag) separate from `tags` (manual/auto),
  `enrichment: {transcript: bool, ocr: bool, vision: bool}`.
- `export.layout: flat | by-category | by-date`; `by-category` →
  `GramVault/<category>/<note>.md`. Re-export moves files when layout or
  category changes (the `_scan_existing_notes_by_gramvault_id` pass already
  finds stragglers).

### G3  Category MOCs
`GramVault/<category>.md` (managed region): the latest digest for that category
(with `[[item:<id>]]` rewritten to `[[<note filename>]]`), then a `dataview`
block `FROM "GramVault/<category>" SORT date DESC` + the plain-table fallback.

### G4  Digests as notes
`POST /api/digests/{id}/export` → `GramVault/_digests/<name>.md` (managed
region; wikilinks). Optionally also update the category MOC.

### G5  Dashboard
`GramVault Dashboard.md`: counts per category, last-30-days, uncategorized,
low-confidence, enrichment failures — Dataview blocks + plain fallback.

### G6  Media
Poster frame for videos (`ffmpeg -ss 1 -frames:v 1`) embedded above a link to
the mp4 — mobile Obsidian handles that far better than inline video.

---

## H. Packaging

### H1  Now: systemd user service (corrected)
```ini
# ~/.config/systemd/user/gramvault.service
[Unit]
Description=GramVault
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=~/gramvault
Environment=GRAMVAULT_CONFIG_PATH=~/gramvault/config.yaml
ExecStart=~/gramvault/.venv/bin/gramvault serve --host 100.x.y.z --port 8777
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```
`systemctl --user daemon-reload && systemctl --user enable --now gramvault`;
`loginctl enable-linger $USER`; logs via `journalctl --user -u gramvault -f`.
If `tailscale0` isn't up yet at boot, the bind fails and `Restart=always`
retries every 5 s until it is — acceptable.

### H2  Later: docker-compose (finish the stubs)
- Dockerfile: multi-stage (`node:22-alpine` build → copy `frontend/dist`),
  `python:3.11-slim` + ffmpeg, `pip install -e ".[instagram]"`, `HF_HOME`
  volume for the whisper model, non-root user.
- Compose: `network_mode: host` for `gramvault` (so the Tailscale-IP bind
  happens in-app and survives boot ordering) — or, if you want a compose
  network, bind `0.0.0.0` inside + set `server.auth_token` + firewall.
  `ollama` service alongside (or `GRAMVAULT_OLLAMA__HOST=http://127.0.0.1:11434`
  to reuse the existing container with host networking). Bind-mount
  `config.yaml`, `secrets.yaml`, `data/`, the vault, and `~/.config/instaloader`
  (ownership: run the container as your uid). `restart: unless-stopped`.
- RAM: app ~100 MB; the constraint is still Ollama + whisper. Don't run
  enrich and digest simultaneously (the single-flight lock per kind won't stop
  that — add a global "heavy job" semaphore of 1).

---

## I. Cost / time estimates (so local-vs-cloud is a decision, not a guess)

Assumptions: ~1814 items, avg ~400 tokens of text each; prices as of 2026-09
public list, rounded.

| Step | Local (this box) | API |
|---|---|---|
| Keyword categorize | instant | — |
| LLM categorize, all items | llama3.2:3b ≈ 1–2 h, accuracy ≈ keyword vote | Haiku ≈ 700k tok in / 60k out ≈ **$0.5–1**, minutes |
| OCR carousels+silent reels (~600 images) | minicpm-v ≈ 5 min/img ≈ **50 h** | Sonnet vision ≈ 600 × ~1.5k tok ≈ **$3–5**, ~30 min |
| Digest, one 200-item category | not viable (3B can't do the reduce) | Sonnet ≈ 120k in / 15k out ≈ **$1–1.5** |
| Re-embed everything | bge-m3 ≈ 30–60 min | OpenAI small ≈ 1M tok ≈ $0.02 (but then you're locked to it) |
| Transcribe (already done) | whisper turbo ≈ 1 video/min | — |

Takeaway: transcription + embeddings stay local; categorize/OCR/digests are
where a key pays for itself; the whole library's worth of cloud work is on the
order of **$10–15** total.

---

## J. Build order (revised)

1. **A1 migrations + A2 WAL/jobs/startup-reset + A4 harness** — one PR, no
   features, unblocks everything and fixes real bugs (F1–F4, F6).
2. **B1 schema + backfill from `categories_final.csv` + B2 filter** — the
   category dropdown works the same day.
3. **A5 providers + E Models/secrets + A3 auth token** — the "API key or local
   model" ask; secrets need auth to exist first.
4. **B3 classifier + review queue + eval** — measure keyword vs LLM on the
   1814 labels before trusting either.
5. **H1 systemd** — stop babysitting (can be done any time; 5 minutes).
6. **C enrichment/OCR tab** — now that `ai.vision` can point at a real
   multimodal model.
7. **D digests** — map/reduce with citations.
8. **G Obsidian** — managed region first (G1), then category frontmatter/layout,
   MOCs, dashboard.
9. **F Instagram pull** — last; riskiest and least coupled.
10. **H2 compose** — package the finished thing; A6 type generation whenever
    the endpoint count gets annoying.

---

## Appendix — endpoint map (new/changed)

| Method | Path | Job kind | Notes |
|---|---|---|---|
| GET | `/api/jobs`, `/api/jobs/{id}` | — | one polling surface |
| POST | `/api/jobs/{id}/cancel` | — | cooperative cancel |
| GET/POST/PATCH/DELETE | `/api/library/categories[/{id}]` | — | rename cascades, delete needs `move_to` |
| GET | `/api/library/items?category=&needs_review=` | — | `__none__` = uncategorized |
| PATCH | `/api/library/items/{id}` | — | `{category_id}` → manual |
| GET | `/api/chat/search?q=&category=` | — | SQL post-filter |
| POST | `/api/categorize/run` | categorize | scope × method |
| POST | `/api/enrich/run` | enrich | steps + ocr_scope; legacy body still accepted |
| GET | `/api/enrich/progress` | — | per-step counts |
| GET | `/api/models`, `/api/models/suggested` | — | key never returned |
| POST | `/api/models/pull`, `/api/models/test` | model_pull | SSE progress |
| PUT | `/api/models/tasks`, `/api/models/secrets` | — | secrets write-only |
| POST | `/api/models/reembed` | reembed | after confirmation |
| GET/POST | `/api/digests[/templates]`, `/api/digests/{id}[/export|/download]` | digest | manifest stored |
| GET/POST | `/api/pull/session`, `POST /api/pull/run` | pull | gated by `pull.enabled` |
| POST | `/api/export/obsidian` | export | `layout`, managed region |
