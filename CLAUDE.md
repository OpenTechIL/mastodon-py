# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Mastodon is a federated social network server implementing ActivityPub. It is a polyglot application:

- **FastAPI backend** (Python 3.12+) — REST API, ActivityPub, federation. Code under `app/python/` (routers, models, services, workers, schemas, auth, federation, policies).
- **Streaming server** (Node 20+) — separate workspace in `streaming/`, serves the WebSocket/EventSource streaming API. Talks directly to PostgreSQL and Redis.
- **Web UI** (React + Redux + TypeScript) — single-page app in `app/javascript/mastodon/`, built by Vite. Mixes legacy `js`/`jsx` with `ts`/`tsx`; new code should be TS.
- **arq workers** — async background job processor; workers live in `app/python/workers/`.

Tech stack: PostgreSQL 14+, Redis 7+, Elasticsearch (optional), FFmpeg 5.1+, libvips. Real-time data flow is typically FastAPI (write) → DB/Redis → arq (fanout) → Streaming server (push to clients) → React UI.

## Common commands

### Running the dev environment

```
bin/dev                 # Boots Procfile.dev: uvicorn (:8000), arq, streaming (:4000), vite, nginx proxy (:3000)
```

Uses `overmind` if installed, otherwise `foreman`.

### Setup / DB

```
bin/setup                                  # Installs Python + JS packages, runs Alembic migrations
uv run alembic upgrade head               # Run pending migrations
uv run alembic revision --autogenerate -m "description"   # Generate new migration
```

### Python tests (pytest)

```
uv run pytest                             # All tests
uv run pytest tests/routers/test_accounts.py    # Single file
uv run pytest tests/routers/test_accounts.py::test_verify_credentials   # Single test
uv run pytest -k "search"                # Tests matching keyword
uv run pytest --tb=short -q              # Quiet mode with short tracebacks
```

### JS/TS tests (Vitest)

```
yarn test:js              # Watch mode (legacy-tests project — anything in __tests__/)
yarn test:js run          # Single run
yarn test:js run path/to/file.test.ts     # Single file
yarn test:storybook       # Storybook component tests (uses Playwright/chromium)
yarn storybook            # Dev storybook on :6006
```

### Linting / typecheck / formatting

```
yarn lint                 # ESLint + Stylelint
yarn fix                  # Auto-fix JS + CSS
yarn typecheck            # tsc --noEmit
yarn format               # oxfmt
uv run ruff check app/python/   # Python linting
uv run ruff format app/python/  # Python formatting
uv run mypy app/python          # Python type checking
```

### i18n

```
yarn i18n:extract                          # Extract frontend strings → en.json
```

## Architecture notes

### Backend layering (don't bypass)

Routers should be thin and delegate to **services** (`app/python/services/`). Side-effecting operations go through a service, not directly in routers or models. Background fan-out work goes through **arq workers** (`app/python/workers/`). Authorization is enforced by **policies** (`app/python/policies/`). JSON API responses use **Pydantic schemas** (`app/python/schemas/`).

### ActivityPub

Federation logic lives under `app/python/federation/`. Inbound activities are processed by `federation/activity.py`. Outbound delivery goes through `workers/delivery.py`. HTTP signatures use RFC 9421 via `federation/signatures.py`. When changing federated behavior, check whether both inbound parsing and outbound serialization need updates.

### Search (Elasticsearch)

Search is optional at runtime — code must degrade gracefully without ES. Index updates are async via arq.

### Frontend (`app/javascript/mastodon/`)

- **Redux store** built with both legacy `redux-immutable` reducers (Immutable.js Records/Maps) and newer `@reduxjs/toolkit` slices. New state should use Toolkit slices and plain JS objects, but legacy Immutable state still dominates timelines, statuses, accounts.
- **Actions** in `actions/` — older files are `.js` thunks dispatching `*_REQUEST` / `*_SUCCESS` / `*_FAIL`; newer `*_typed.ts` use `createAppAsyncThunk`. Mirror existing style in the file you're editing.
- **Features** in `features/` are route-level views; `containers/` are connected wrappers; `components/` are presentational. `react-router` v5 routes live in `features/ui/`.
- **API** calls go through `api.ts` (axios instance with auth + base URL). Don't fetch directly.
- **i18n**: wrap user-facing strings with `react-intl` (`<FormattedMessage>` / `defineMessages`). After adding strings, run `yarn i18n:extract`.
- **Models**: `models/` contains both Immutable Record factories (older) and plain TS types (newer).

### Streaming server (`streaming/`)

Separate yarn workspace, separate `tsconfig.json` and `eslint.config.mjs`. Reads directly from PG (`streaming/database.js`) and subscribes to Redis pub/sub channels published by FastAPI. When adding a new timeline/stream, both FastAPI publish path (`services/fanout.py`) and the streaming server's channel handling need updates.

### Database conventions

Use Alembic for all migrations (`alembic/`). For large tables use `CREATE INDEX CONCURRENTLY` and batched backfills. Never write long-locking DDL.

## Python backend layout

- `app/python/` — FastAPI app. `main.py` (factory), `settings.py` (reads deployment ENV vars), `db.py` (async SQLAlchemy), `deps.py` (FastAPI deps), `common/` (snowflake, discard, pagination, counter_cache).
- `alembic/` — async Alembic env pointing at Postgres.
- `tests/` — pytest (asyncio_mode=auto). `tests/common/test_snowflake.py` is the parity test for ID generation — keep it green.
- `pyproject.toml` + `uv.lock` — managed with [`uv`](https://docs.astral.sh/uv/). Install: `uv sync`. Add a runtime dep: `uv add <pkg>`. Add a dev dep: `uv add --group dev <pkg>`.

Ad-hoc commands inside the venv go through `uv run` — e.g. `uv run pytest`, `uv run mypy app/python`, `uv run alembic upgrade head`.

When adding a new endpoint: translate implicit side-effects into explicit service calls (do NOT use SQLAlchemy events for business logic), and add tests in `tests/routers/`.

## Conventions and gotchas

- Node pinned by `.nvmrc` (24.15). Use nvm or equivalent.
- The `test` yarn script runs `lint + typecheck + test:js run` — that's the full JS pre-PR check.
- Don't commit changes to `app/javascript/mastodon/locales/*.json` other than `en.json` — translations come from Crowdin.
- System specs require Chrome/Chromium and a running streaming server.
- Mastodon is **AGPLv3**. AI-assisted contributions are governed by the project's [AI Contribution Policy](https://github.com/mastodon/.github/blob/main/AI_POLICY.md).
