# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Mastodon is a federated social network server implementing ActivityPub. This is a fork migrating from Ruby on Rails to Python FastAPI. It is a polyglot application:

- **FastAPI backend** (Python 3.12+) — REST API, ActivityPub, federation. Code under `app/python/` (routers, models, services, workers, schemas, auth, federation, policies).
- **Streaming server** (Node 20+) — separate workspace in `streaming/`, serves the WebSocket/EventSource streaming API. Talks directly to PostgreSQL and Redis.
- **Web UI** (React + Redux + TypeScript) — single-page app in `app/javascript/mastodon/`, built by Vite. Mixes legacy `js`/`jsx` with `ts`/`tsx`; new code should be TS.
- **arq workers** — async background job processor; workers live in `app/python/workers/`.

Tech stack: PostgreSQL 14+, Redis 7+, Elasticsearch (optional), FFmpeg 5.1+, libvips. Real-time data flow: FastAPI (write) → DB/Redis → arq (fanout) → Streaming server (push to clients) → React UI.

## Dev environment

```
bin/dev    # Boots Procfile.dev via overmind/foreman
```

Procfile.dev processes and ports:

- `python` — uvicorn FastAPI on `:8000`
- `arq` — background job worker
- `stream` — Node streaming server on `:4000`
- `vite` — Vite dev server on `:3036`
- `proxy` — nginx on `:3000` (public entry point)

**Nginx routing** (`config/nginx/dev.conf`):

- `/packs-dev/` → Vite (with HMR WebSocket)
- `/api/v1/streaming` → streaming server (WebSocket)
- Everything else → FastAPI

**After changing Python code in Docker**: always rebuild and restart:

```bash
docker compose build python && docker compose up -d python && sleep 5 && docker compose restart proxy
```

## Common commands

### Setup / DB

```bash
bin/setup                                         # Install Python + JS packages, run Alembic migrations
uv run alembic upgrade head                       # Run pending migrations
uv run alembic revision --autogenerate -m "desc"  # Generate new migration
```

### Python tests (pytest)

```bash
uv run pytest                                                    # All tests
uv run pytest tests/routers/test_accounts.py                     # Single file
uv run pytest tests/routers/test_accounts.py::test_verify_credentials  # Single test
uv run pytest -k "search"                                        # Keyword match
uv run pytest --tb=short -q                                      # Quiet + short tracebacks
```

### JS/TS tests (Vitest)

```bash
yarn test:js run                                  # Single run (lint + typecheck + tests)
yarn test:js run path/to/file.test.ts             # Single file
yarn test:storybook                               # Storybook component tests
yarn storybook                                    # Dev storybook on :6006
```

### Linting / typecheck / formatting

```bash
yarn lint && yarn typecheck        # Full JS pre-PR check
yarn fix                           # Auto-fix JS + CSS
uv run ruff check app/python/      # Python linting
uv run ruff format app/python/     # Python formatting
uv run mypy app/python             # Python type checking
```

### i18n

```bash
yarn i18n:extract    # Extract frontend strings → en.json
```

## Python backend architecture

### Layering (strict — don't bypass)

- **Routers** (`routers/`) — thin; delegate side effects to services
- **Services** (`services/`) — all business logic and DB mutations; can enqueue jobs
- **Policies** (`policies/`) — authorization checks; called from routers before services
- **Schemas** (`schemas/`) — Pydantic response models; `serialize_account()` / `serialize_status()` are the canonical serializers
- **Workers** (`workers/`) — arq background jobs (delivery, fanout, media processing)
- **Models** (`models/`) — SQLAlchemy ORM; no business logic

### Key files

- `main.py` — FastAPI app factory, mounts all routers, registers static file mounts, lifespan hook
- `settings.py` — reads all env vars via `@lru_cache`; reset in tests with `get_settings.cache_clear()`
- `deps.py` — FastAPI `Depends()` chain: `DBSession`, `BearerToken` → `OptionalAuth` → `CurrentAuth` → `CurrentAccount` / `CurrentUser`
- `db.py` — async SQLAlchemy engine; `dispose_engine()` called on shutdown

### Static file mount order (critical)

`main.py` mounts in this order — later mounts won't shadow earlier ones:

1. All API/auth/federation routers
2. Named public subdirs: `/sounds/`, `/emoji/`, `/avatars/`, `/headers/`, `/packs/`, `/system/`, `/ocr/`
3. SPA catch-all last: `GET /` and `GET /{path:path}` → React shell HTML

The SPA shell (`routers/web.py`) also handles `GET /web/{path:path}` → 302 redirect to `/{path}` (mirroring the original Rails route so React Router sees clean paths without a `/web` basename).

### Authentication

Two auth paths resolve to the same `AuthContext(access_token, application, user, account)`:

1. **API** — `Authorization: Bearer <token>` header, validated by `auth/tokens.py`. Checks: exists, not revoked, not expired, user is functional (confirmed + approved + not disabled).
2. **Browser SPA** — `_mastodon_session` cookie (HMAC-signed base64 JSON containing `user_id`, `account_id`, `token`). Read by `routers/web.py` to embed token in `initial-state` so the React SPA bootstraps with auth.

`OptionalAuth` returns `None` for unauthenticated requests. It only reads Bearer tokens, not the session cookie. Session cookies are only consumed by `routers/web.py` and `routers/auth_web.py`.

Scope check: `has_scope("read:statuses")` passes for both `read:statuses` and parent scope `read`.

### Settings / domain config

Key env vars (see `settings.py` for full list):

- `LOCAL_DOMAIN` — canonical hostname (default `localhost:3000`)
- `WEB_DOMAIN` — optional CDN/public alias; `effective_web_domain` returns this if set, else `LOCAL_DOMAIN`
- `RAILS_ENV` / `NODE_ENV` — controls scheme (`https` in production, `http` otherwise)
- `MEDIA_ROOT` — filesystem path for local media storage (default `public/system`)
- `S3_ENABLED`, `S3_BUCKET`, `S3_REGION`, … — S3 config; if enabled without bucket/region, `get_storage()` raises immediately

URL helpers: `settings.base_url("/path")` → `<scheme>://<effective_web_domain>/path`.

### Media storage

`app/python/storage/__init__.py` provides a `Storage` protocol with `write(key, data)`, `read(key)`, `url(key)`.

- **LocalStorage** — writes to `<media_root>/<key>`, serves at `<scheme>://<effective_web_domain>/system/<key>`
- **S3Storage** — aiobotocore-backed; public URLs use `alias_host` (CDN) if set

Storage key convention (Paperclip-style):

- Media: `media_attachments/files/<account_id>/<variant>/<filename>`
- Avatars: `accounts/avatars/<account_id>/{original,static}/<filename>`
- Headers: `accounts/headers/<account_id>/{original,static}/<filename>`

When uploading avatar/header, always write **both** `original/` and `static/` paths — the SPA requests both URLs.

`app/python/lib/asset_urls.py` builds public URLs:

- If `remote_url` set → use it as-is (remote actor)
- If `file_name` set → `/system/accounts/{kind}s/{id}/{size}/{file_name}`
- Otherwise → fallback missing-asset URL (`/avatars/original/missing.png`, etc.)

### Multipart vs JSON in profile/account endpoints

`PATCH /api/v1/profile` and `PATCH /api/v1/accounts/update_credentials` accept **both** content types:

- `multipart/form-data` — sent by the SPA when uploading avatar/header files
- `application/json` — sent by API clients and tests

Pattern used (detect at runtime):

```python
content_type = request.headers.get("content-type", "")
if "multipart/form-data" in content_type:
    form = await request.form()
    data = {k: v for k, v in form.items() if not hasattr(v, "read")}
    # file fields have .read(); check hasattr, not truthiness
else:
    data = json.loads(await request.body())
```

### ORM conventions

All relationships use `lazy="joined"` (single SQL JOIN, not deferred SELECT). This means:

- Post-query, related objects are already populated — serializers can access them directly
- After **manually constructing** a model (not from a query), related fields are NOT populated — call `await session.refresh(obj)` after commit before serializing

`Discardable` mixin adds `deleted_at` for soft-deletes. **No implicit filter** — queries must explicitly add `Status.kept_clause()` / `.where(Status.deleted_at.is_(None))`. This is intentional to catch missing filters in code review.

Counter-cache updates use `common/counter_cache.py → adjust_counter(session, table, row_id, column, delta)` which wraps the UPDATE in a Postgres advisory lock to prevent race conditions.

### Snowflake IDs

48-bit millisecond timestamp + 16-bit tail (DB sequence bits). `common/snowflake.py → now_id()` is the Python-side generator used in tests. Production uses the Postgres `timestamp_id(<table>)` function. `tests/common/test_snowflake.py` validates parity — keep it green.

### Pagination cursors

- `max_id` → `id < max_id` (older)
- `min_id` → `id > min_id`, ascending, reversed by caller ("load newer")
- `since_id` → `id > since_id`, ascending (not used for "load newer")
- Link header built from first/last IDs in result: `next=max_id=<last>`, `prev=min_id=<first>`

### ActivityPub / federation

Federation module map:

| File                           | Role                                                                                                                        |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `federation/activity.py`       | Inbound activity dispatch (`Create`, `Follow`, `Accept`, `Reject`, `Undo`, `Delete`, `Like`, `Announce`, `Update`, `Block`) |
| `federation/serializers.py`    | Outbound AP JSON: `serialize_note()`, `serialize_create_activity()`                                                         |
| `federation/signatures.py`     | HTTP signatures (draft-cavage-10). `sign_request()` / `verify_request(now=)`                                                |
| `federation/signed_request.py` | Full inbound verification pipeline: parse → resolve key → verify                                                            |
| `federation/actor_fetcher.py`  | Fetch + persist a remote actor on first contact                                                                             |
| `federation/key_resolver.py`   | Resolve `keyId` → PEM; local DB first, HTTP fallback                                                                        |
| `federation/keys.py`           | RSA keypair generation for local actors                                                                                     |
| `federation/fanout.py`         | Deliver one activity to many recipient inboxes (batch)                                                                      |
| `workers/delivery.py`          | arq job: sign + POST a single activity to a remote inbox                                                                    |

Key invariants:

- Signature covers `(request-target) host date digest content-type`; digest is SHA-256 of body.
- `verify_request()` accepts an optional `now=` parameter — pass it in tests that use a frozen sign time to avoid the ±12 h date-skew rejection.
- `activity.py` maintains an in-process activity-ID dedup set (`_SEEN_ACTIVITY_IDS`). Tests that dispatch the same activity ID across multiple test functions must call `clear_activity_dedup_cache()` in an autouse fixture, or use unique IDs.
- `serialize_note()` includes `@context` so standalone Note object fetches (peer dereferencing a URI) are valid AP.

When changing federated behavior, update both the inbound parser (`federation/activity.py`) and outbound serializer (`federation/serializers.py`).

### Job queue

`queue.py` defines `Enqueuer` protocol: `async def enqueue(function_name, *args)`. Tests use `FakeEnqueuer` which records `(function_name, args)` tuples for assertion without Redis.

## Frontend architecture

- **Redux store** — legacy `redux-immutable` Immutable.js Records + newer `@reduxjs/toolkit` slices. New state → Toolkit slices + plain JS. Legacy Immutable state dominates timelines/statuses/accounts.
- **Actions** — older `.js` files dispatch `*_REQUEST` / `*_SUCCESS` / `*_FAIL`; newer `*_typed.ts` use `createAppAsyncThunk`. Mirror existing style per file.
- **Routes** — React Router v5 in `features/ui/index.jsx`. Routes are clean paths without `/web` prefix.
- **API calls** — all go through `api.ts` (axios instance); don't fetch directly.
- **Modal system** — `features/ui/components/modal_root.jsx` renders modals via `Bundle` (lazy loader). Only passes `ref` to class components and `forwardRef` components — not to plain function components.
- **i18n** — wrap user-visible strings with `react-intl`. Run `yarn i18n:extract` after adding strings. Don't commit non-`en.json` locale files (Crowdin manages them).
- **Theme toggle** — appearance preference (`dark` / `light` / `auto`) is stored in Redux settings under `appearance.colorScheme`. `selectAppearanceColorScheme` in `selectors/settings.ts` reads it; `ThemeCycleButton` (home column header) and `ThemeToggle` (navigation panel) let users cycle modes. CSS custom properties (`--color-bg-primary`, `--color-border-primary`, `--dropdown-shadow`, etc.) defined in `styles/mastodon/theme/_light.scss` and `_dark.scss` must be updated together when adding theme-aware components. The `auto` mode applies the OS `prefers-color-scheme` media query.

## Test setup

- **SQLite in-memory** — all Python tests use `:memory:` SQLite with raw schema creation (not Alembic migrations). `Base.metadata.create_all()` at fixture setup.
- **Fixtures** (`tests/conftest.py`) — `seed_data` dict of factory functions (`make_account`, `make_user`, `make_token`, `make_status`, …); call them and `session.add()` manually per test.
- **FakeEnqueuer** — replaces Redis enqueue; assert on `fake_enqueuer.calls` list of `(function_name, args)`.
- **bcrypt rounds** — tests use `rounds=4` (production uses 12) for speed.
- `tests/common/test_snowflake.py` — parity test for ID generation; must stay green.

## Conventions and gotchas

- Node pinned by `.nvmrc` (24.15). Use nvm or equivalent.
- `yarn test` runs `lint + typecheck + test:js run` — the full JS pre-PR check.
- `pyproject.toml` + `uv.lock` managed with `uv`. Add runtime dep: `uv add <pkg>`. Dev dep: `uv add --group dev <pkg>`.
- Don't commit locale files other than `en.json`.
- Mastodon is **AGPLv3**. AI-assisted contributions are governed by the project's [AI Contribution Policy](https://github.com/mastodon/.github/blob/main/AI_POLICY.md).
