# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Use `uv`, never `pip`.

```bash
uv sync                         # install/sync dependencies
uv run ruff check .             # lint
uv run ruff format .            # format
uv run pytest                   # full test suite
uv run pytest tests/test_olx.py::test_build_title_wg_sprawdzonego_szablonu  # single test
uv run pytest -k "publish_item" # tests matching a substring
```

Run the app (do NOT use plain `uvicorn` — see below):

```bash
uv run python -m zibicom
```

`psycopg` 3 can't run on the `ProactorEventLoop` uvicorn defaults to on Windows
(`InterfaceError: Psycopg cannot use the 'ProactorEventLoop'`). `src/zibicom/__main__.py`
forces a `SelectorEventLoop` via `loop_factory`. Same trap applies to integration tests on
Windows.

Local Postgres for dev/tests: `docker compose up -d db` (service name `db`, exposed on
`127.0.0.1:5432`). Password comes from `secrets/postgres_password.txt`.

## Test database — hard safety gate

Tests connect to a real Postgres (no mocked DB — enums/CHECKs/FK cascades from migrations are
part of the contract) and `db_session` (`tests/conftest.py`) does `DELETE FROM` on
`intake_batch`/`listing`/`game`/`olx_token`/`olx_operation` after every test. This **must never**
run against the app's real database.

- Test DB name must end in `_test` (default `zibicom_test`; override via `TEST_POSTGRES_DB`,
  which must still end in `_test`). `conftest.py` refuses to run otherwise, and re-checks
  `SELECT current_database()` immediately before every `DELETE` as a second, independent guard.
- The test DB is created and migrated automatically on first `pytest` run. Migrations are **not
  idempotent** (written for one-shot execution via `docker-entrypoint-initdb.d`), so after adding
  a new migration file you must drop it to force a re-migrate:
  `docker compose exec db psql -U zibicom -d postgres -c 'DROP DATABASE zibicom_test'`.

## Architecture

**Pipeline (end to end):** photo upload → R2 → Gemini vision recognition → grouping into
physical copies → intake staging (human review/correction) → approval → OLX publish → promotion
to production tables (`game`/`listing`/`listing_photo`).

**Module split** (`src/zibicom/`):
- `olx.py` — pure OLX Partner API integration only (HTTP, auth, payload building). Knows nothing
  about our business tables.
- `intake.py` — business orchestration: staging tables, approval workflow, and all writes to
  `game`/`listing`/`listing_photo`. Calls into `olx.py` but owns the transactions.
- `routers.py` — FastAPI endpoints, thin wrappers translating domain exceptions
  (`IntakeError`/`olx.OlxError` subclasses) to `HTTPException`.
- `photos.py` / `vision.py` / `grouping.py` / `models.py` — the recognition pipeline (R2
  upload/normalize, Gemini calls, grouping recognized photos into one row per physical copy).
- `crypto.py` — Fernet encryption for OLX tokens at rest; the key is a secret and never touches
  the database.
- `config.py` — `pydantic-settings`, three source tiers in priority order: env vars → `.env` →
  `/run/secrets/*` (Docker Secrets). `/run/secrets` detection is dynamic (absent outside Docker).

**Publish/preview share one payload builder.** `intake._build_advert_payload_for_item` (calls
`olx.build_title`/`build_description`/`build_advert_payload`) is used by both
`publish_item` (writes + `create_advert`) and `preview_publish_item`
(`GET /api/intake/items/{id}/publish/preview`, read-only, no OLX call, any item status) — keep
them sharing this function so preview stays a faithful diagnostic of what publish would send.
Platform/category resolution (`_resolve_platform_for_publish`) is a separate, OLX-free DB lookup
called *before* `olx.get_access_token` in `publish_item`, so a platform missing an OLX category
fails fast without requiring valid OLX auth first — don't collapse that ordering when refactoring.

**OLX status is not fire-and-forget.** OLX can move an advert through
`new`/`waiting`/`moderated`/`active`/`disabled`/etc. without our involvement (moderation,
`valid_to` expiry, manual removal). `intake._map_olx_status` maps OLX's raw status to our
`listing_status` enum; unknown values fall back to `'pending'` with a logged warning rather than
crashing. `intake.sync_advert_status` (`POST /api/listings/{id}/sync-status`) re-fetches and
re-maps on demand — call it, don't assume the status written at publish time is still current.

**Migrations are additive and mostly non-idempotent.** Never edit a shipped migration file;
add a new numbered one. The exception is the OLX category/attribute data-mapping migrations
(`0005`–`0007`), which are deliberately idempotent (`ADD COLUMN IF NOT EXISTS`, unconditional
`UPDATE`, no `DROP`/`DELETE`) since they encode empirically-verified OLX values that may need
re-running as more categories get confirmed.

**HTTP clients are process-lifetime singletons.** `olx._http_client()` / `_partner_http_client()`
and the Gemini client in `vision.py` are built once via `@lru_cache` and reused — recreating them
per-call causes `RuntimeError: client has been closed` from GC'd transports. `Settings` fields
holding secrets are `SecretStr`; call `.get_secret_value()` to get the plain string.

**OLX Partner API quirks** (see `olx.py` docstrings/comments for the empirical evidence):
requires a custom `User-Agent` (CloudFront blocks the httpx default with an empty 403), requires
`Version: 2.0` on Partner API calls but not on OAuth, wraps all responses in a `"data"` key,
paginates `/cities` via `limit`/`offset` with no metadata (max `limit` 10000), and rejects
`auto_extend_enabled`/`ad_delivery` on write even though they appear on read. OLX has no test
environment — every `create_advert` call is a real listing.
