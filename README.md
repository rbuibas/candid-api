# candid-api

Python/FastAPI backend for **Candid** — a private, invite-only, event-scoped group
camera app. Supabase (Postgres + auth + RLS) · Cloudflare R2 (media) · FCM (push) ·
Resend (email) · Render (web + cron workers). See the workspace `docs/` for the
product spec and build phases; `CLAUDE.md` holds the coding guardrails.

## Prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/) for dependency + venv management
- A Supabase project and the env vars in `.env` (see `.env.example` if present;
  `SUPABASE_*`, `R2_*`, etc.)

## Run / build / lint commands

| Command | What it does |
| ------- | ------------ |
| `uv sync` | Install dependencies into `.venv` |
| `uv run uvicorn app.main:app --reload` | Run the API locally |
| `uv run pytest` | Run the test suite |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Formatting check (`--check` off to apply) |

## Validation

Validation is two-tier: automated tests here (no live database or external
services), plus the shared manual end-to-end scenarios at the workspace root.

**Automated (`uv run pytest`).** Unit tests under `tests/` and DB-backed
integration tests under `tests/integration/`. Router/service tests use a
**mocked Supabase client** (`unittest.mock`) and a self-signed JWT, so they run
fully offline — no Postgres, R2, or FCM required. Priorities are the
correctness-critical paths: the prompt state machine, idempotent `confirm`, RLS
policy behaviour, and request validation.

E1 (the `client_events` / `POST /events` plumbing) is covered by:

| Test | What it locks in |
| ---- | ---------------- |
| `tests/test_events.py` | `POST /events`: member → 201 (row owned by the caller, not the client-supplied id), default-empty + verbatim/nested payload, **non-member → 404** (anti-leak), unauth → 401, name length bounds (1–64) → 422, invalid/missing `group_id` → 422 |
| `tests/test_client_events_migration.py` | Static assertions on the migration SQL (no DB): column + FK-cascade contract, **RLS enabled**, policies **mirror `posts`** (member-read, self+member-insert), and **no client UPDATE/DELETE** policy |

Run a focused slice while iterating:

```bash
uv run pytest tests/test_events.py tests/test_client_events_migration.py -q
```

> Note: if `uv run pytest` ever fails to launch the test binary on your machine
> (a `uv` trampoline quirk on some Windows setups), use `uv run python -m pytest`.

**Migrations** live in `supabase/migrations/` and must be applied to the
Supabase project for the live API to work — the migration test only checks the
SQL's shape, not that it has been run.

**Manual end-to-end.** Repo-agnostic device-level regression scenarios live at
the workspace root in [`e2e/`](../e2e); they verify the API and app together
(e.g. that `feed_opened` rows actually land server-side). Run the E1 set after
changes that touch events, groups, or auth.

## Layout

```
src/app/
  main.py            FastAPI app factory + router registration
  config.py          settings (EU region defaults)
  auth/              Supabase JWT verification (get_current_user[_id])
  routers/           thin HTTP handlers (groups, feed, posts, events, ...)
  services/          business logic (membership-scoped queries; RLS is the 2nd line)
  models/            Pydantic v2 request/response models
  clients/           Supabase / R2 / Firebase / Resend
  workers/           generator · dispatcher · expirer (Render cron entrypoints)
supabase/migrations/ SQL migrations (schema + RLS)
tests/               unit (mocked Supabase) + tests/integration (DB-backed)
```
