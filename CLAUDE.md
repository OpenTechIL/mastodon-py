# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Mastodon is a federated social network server implementing ActivityPub. It is a polyglot application made up of three runtime processes plus background workers:

- **Rails app** (Ruby 3.3+, Rails) — REST API, web pages, ActivityPub. Code under `app/` (controllers, models, services, workers, serializers, presenters, policies, chewy ES indexes).
- **Streaming server** (Node 20+) — separate workspace in `streaming/`, serves the WebSocket/EventSource streaming API. Talks directly to PostgreSQL and Redis.
- **Web UI** (React + Redux + TypeScript) — single-page app in `app/javascript/mastodon/`, built by Vite. Mixes legacy `js`/`jsx` with `ts`/`tsx`; new code should be TS.
- **Sidekiq** — background job processor; workers live in `app/workers/`.

Tech stack: PostgreSQL 14+, Redis 7+, Elasticsearch (optional, via Chewy), FFmpeg 5.1+, libvips. Real-time data flow is typically Rails (write) → DB/Redis → Sidekiq (fanout) → Streaming server (push to clients) → React UI.

## Common commands

### Running the dev environment
```
bin/dev                 # Boots Procfile.dev: puma (Rails :3000), sidekiq, streaming (:4000), vite
```
Uses `overmind` if installed, otherwise `foreman`. Default admin: `admin@mastodon.local` / `mastodonadmin`.

### Setup / DB
```
bin/setup                                  # Installs gems + node packages, prepares DB
bin/rails db:setup                         # Initial DB setup
bin/rails db:migrate                       # Run migrations
bin/rails dev:populate_sample_data         # Populate with @showcase_account etc.
bin/tootctl <command>                      # Admin CLI (accounts, domains, media, search)
```

### Ruby tests (RSpec)
```
bin/rspec                                  # All non-system specs
bin/rspec spec/models/account_spec.rb      # Single file
bin/rspec spec/models/account_spec.rb:42   # Single example by line
bin/rspec spec/system --tag streaming --tag js   # System specs that require streaming + JS
bin/rspec --tag search                     # Search-related (needs Elasticsearch)
bin/flatware rspec                         # Parallel runner used in CI
```
First run any new spec layout with `bin/flatware fan bin/rails db:test:prepare` if needed.

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
bundle exec rubocop       # Ruby
bundle exec haml-lint app/views     # HAML
bundle exec brakeman      # Security scan
bundle exec i18n-tasks normalize    # Normalize locale files (required before PR)
```

### i18n
```
yarn i18n:extract                          # Extract frontend strings → en.json
bundle exec i18n-tasks missing             # Find missing backend translations
```

## Architecture notes

### Backend layering (don't bypass)
Controllers should be thin and delegate to **services** (`app/services/*_service.rb`). Side-effecting operations — anything that touches federation, multiple records, or external state — go through a service object, not directly in controllers or models. Background fan-out work goes through **workers** (`app/workers/`). Authorization is enforced by **policies** (`app/policies/`, Pundit). JSON API responses use **serializers** (`app/serializers/`). Heavy view-prep logic lives in **presenters**.

### ActivityPub
Federation logic lives under `app/services/activitypub/`, `app/lib/activitypub/`, `app/workers/activitypub/`, and `app/serializers/activitypub/`. Inbound activities are processed by `ActivityPub::ProcessCollectionService` → `Activity::*` handler classes. Outbound delivery goes through `ActivityPub::DeliveryWorker`. When changing federated behavior, check whether both sides (inbound parse + outbound serialization) need updates.

### Search (Chewy / Elasticsearch)
`app/chewy/*_index.rb` defines ES indexes. Search is optional at runtime — code must degrade gracefully without ES. Index updates happen via `Chewy.strategy(:sidekiq)` async, so test code may need `Chewy.strategy(:atomic)`.

### Frontend (`app/javascript/mastodon/`)
- **Redux store** built with both legacy `redux-immutable` reducers (Immutable.js Records/Maps) and newer `@reduxjs/toolkit` slices. New state should use Toolkit slices and plain JS objects, but legacy Immutable state still dominates timelines, statuses, accounts.
- **Actions** in `actions/` — older files are `.js` thunks dispatching `*_REQUEST` / `*_SUCCESS` / `*_FAIL`; newer `*_typed.ts` use `createAppAsyncThunk`. Mirror existing style in the file you're editing.
- **Features** in `features/` are route-level views; `containers/` are connected wrappers; `components/` are presentational. `react-router` v5 routes live in `features/ui/`.
- **API** calls go through `api.ts` (axios instance with auth + base URL). Don't fetch directly.
- **i18n**: wrap user-facing strings with `react-intl` (`<FormattedMessage>` / `defineMessages`). After adding strings, run `yarn i18n:extract`.
- **Models**: `models/` contains both Immutable Record factories (older) and plain TS types (newer).

### Streaming server (`streaming/`)
Separate yarn workspace, separate `tsconfig.json` and `eslint.config.mjs`. Reads directly from PG (`streaming/database.js`) and subscribes to Redis pub/sub channels published by Rails. When adding a new timeline/stream, both Rails publish path (`FanOutOnWriteService` and friends) and the streaming server's channel handling need updates.

### Database conventions
Use the helpers in `lib/mastodon/migration_helpers.rb` for safe migrations on large tables (concurrent index, batched backfills). Never write raw long-locking DDL in migrations.

## Python backend (in progress)

A phased FastAPI + async backend is under way. Plan: `/Users/yehuda/.claude/plans/convert-rails-app-to-jazzy-lobster.md`. Each phase is its own delivery; the legacy backend keeps serving unported paths behind an nginx reverse proxy (`config/nginx/dev.conf`).

Layout:
- `app/python/` — FastAPI app. `main.py` (factory), `settings.py` (reads deployment ENV vars), `db.py` (async SQLAlchemy bound to the existing schema), `deps.py` (FastAPI deps), `common/` (snowflake, discard, pagination, counter_cache).
- `alembic/` — async Alembic env pointing at the same Postgres.
- `tests/` — pytest (asyncio_mode=auto). `tests/common/test_snowflake.py` is the parity test for ID generation — keep it green; everything downstream depends on snowflake ID layout compatibility.
- `pyproject.toml` + `uv.lock` — managed with [`uv`](https://docs.astral.sh/uv/). Install: `uv sync`. Add a runtime dep: `uv add <pkg>`. Add a dev dep: `uv add --group dev <pkg>`.

Run: `bin/dev` starts `python` (uvicorn :8000), `arq`, and `proxy` (nginx :3000) alongside the legacy backend on :3001. The proxy currently forwards only `/_py/*` to FastAPI; promote paths there as each phase ships. Ad-hoc commands inside the venv go through `uv run` — e.g. `uv run pytest`, `uv run mypy app/python`, `uv run alembic upgrade head`.

When porting a subsystem: translate implicit `after_*` hooks into explicit service calls (do NOT use SQLAlchemy events for business logic), and add a contract test in `tests/contract/` diffing JSON bodies against the legacy endpoint before promoting in the proxy.

## Conventions and gotchas

- **Ruby version pinned by `.ruby-version` (4.0.3)**; Node by `.nvmrc` (24.15). Use rbenv/nvm or equivalent.
- The `test` yarn script runs `lint + typecheck + test:js run` — that's the full JS pre-PR check.
- Locale files must be normalized (`i18n-tasks normalize` for backend; extract for frontend) or CI fails.
- Don't commit changes to `app/javascript/mastodon/locales/*.json` other than `en.json` — translations come from Crowdin.
- `bin/tootctl` is the admin/maintenance CLI — prefer it over ad-hoc rails runner scripts.
- System specs require Chrome/Chromium and a running streaming server for `--tag streaming` cases.
- Mastodon is **AGPLv3**. AI-assisted contributions are governed by the project's [AI Contribution Policy](https://github.com/mastodon/.github/blob/main/AI_POLICY.md).
