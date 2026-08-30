# GigHound — Environment Variables

All configuration is env-driven; nothing is hardcoded in application logic.
Run `./scripts/bootstrap.sh` (creates `.env` from `.env.example` and fills
`GIGHOUND_SECRET_KEY`, `GIGHOUND_VAULT_KEY`, `GIGHOUND_WORKER_TOKEN`, and
`POSTGRES_PASSWORD` with generated values — idempotent, never overwrites real
values), or copy `.env.example` and export these before starting the backend.

## Text generation (Ollama / LLM)

| Variable | Default | Notes |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://ollama:11434/v1` in Docker, `http://localhost:11434/v1` on a local machine | OpenAI-compatible Ollama endpoint. Docker is auto-detected via `/.dockerenv` (or force with `GIGHOUND_IN_DOCKER=1`). Any other host (LAN box, remote GPU) must be set explicitly. |
| `OLLAMA_MODEL` | `qwen3:4b` | Model tag as known to Ollama (`ollama list`). |
| `OLLAMA_TEMPERATURE` | `0.7` | Default sampling temperature; per-call override supported. |
| `OLLAMA_MAX_TOKENS` | `1024` | Default completion cap; per-call override supported. |
| `OLLAMA_TIMEOUT` | `120` | Seconds. Generation budget; connect fails fast at 5s so offline fallback triggers quickly when Ollama is down. |
| `OLLAMA_REASONING_HEADROOM` | `6144` | Extra token budget added to Ollama requests for reasoning models (qwen3 burns hidden reasoning tokens inside the same `max_tokens` budget — measured ~5.2k reasoning tokens for a 150-word answer). Set `0` for non-reasoning models. |
| `LLM_PROVIDER` | auto | Force `ollama` or `openai`. Auto: explicit `OLLAMA_BASE_URL` → ollama; else `LLM_API_KEY` → openai; else ollama with the default URL. |
| `LLM_API_KEY` | — | Hosted provider key (OpenAI-compatible). |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Hosted provider base URL. |
| `LLM_MODEL` | `gpt-4o-mini` | Hosted provider model. |
| `LLM_MAX_RPM` | `60` | Token-bucket rate limit across all generation. |

If no provider is reachable, generation falls back to the deterministic
offline composer (proposals) or offline template stubs (profile/proposal
templates) — the API stays functional and marks responses `"offline": true`.

### Where generation is used
- Proposal drafts: automatic, via the orchestrator (`/api/jobs/ingest` pipeline)
- Profile pitch templates: `POST /api/profiles/templates/generate` `{platform, notes, temperature?, max_tokens?, timeout?}`
- Proposal templates: `POST /api/proposals/templates/generate` `{platform, skills[], tone, save?, ...}`
- Fiverr gig FAQs: `POST /api/gigs/faqs/generate`

## Infrastructure

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://gighound:gighound@localhost:5432/gighound` | SQLAlchemy URL; SQLite works for dev (`sqlite:////tmp/gh.db`). |
| `POSTGRES_PASSWORD` | — | **Required by `docker-compose.yml`** (compose refuses to start without it). Postgres and Redis ports bind to `127.0.0.1` only. |
| `REDIS_URL` | `redis://localhost:6379/0` | Cache, rate buckets, circuit breaker, openings pool. Graceful no-op fallback when down. |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated. |
| `CACHE_TTL_SECONDS` | `300` | TTL for the Redis-backed response cache; graceful no-op when Redis is down. |
| `GIGHOUND_VAULT_KEY` | — | Fernet key for the credential vault. **Mandatory outside dev mode**: first vault use fails fast without it, unless `GIGHOUND_DEV_NOAUTH=1` (dev then generates a key persisted to `backend/.vault-dev-key`, mode 0600, gitignored). Legacy alias `GIGHUNTER_VAULT_KEY` accepted. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

## Auth & tenancy

Every row in the tenant-owned tables carries `user_id`; all API endpoints
except the public-by-design ones (`GET /api/health`, `POST /api/auth/register`,
`POST /api/auth/login`) require `Authorization: Bearer <jwt>`. The WebSocket
`/ws/alerts` takes the token as `?token=` (browsers can't set WS headers) and
broadcasts are per-user. `POST /api/auth/password` (change password) and
`DELETE /api/auth/account` (password-verified account deletion; every tenant
FK is `ON DELETE CASCADE`, so the user-row delete wipes all tenant data) are
JWT-protected and drive the user-menu account flows in the SPA.

| Variable | Default | Notes |
|---|---|---|
| `GIGHOUND_SECRET_KEY` | — | HS256 JWT signing secret (12h access tokens). Startup **fails fast** when unset, unless `GIGHOUND_DEV_NOAUTH=1`. |
| `GIGHOUND_DEV_NOAUTH` | off | `1` disables auth: every request runs as a single implicit dev user (`dev@gighound.local`). Local development only — never set in production. |
| `GIGHOUND_ALLOW_REGISTRATION` | `true` | `false` closes `POST /api/auth/register` (self-hosted default is open; set false for a closed SaaS). |
| `GIGHOUND_WORKER_TOKEN` | — | Shared token for the stealth-browser worker pool (AD-4), separate from user JWTs. Gates stealth-task claim/complete and worker result-posting endpoints; startup **fails fast** when unset, unless `GIGHOUND_DEV_NOAUTH=1`. Set the same value in the worker's environment (see `worker/README.md`). |

Schema is managed by Alembic: run `alembic upgrade head` (from `backend/`)
before starting the API. `create_all` no longer runs on startup. The demo
seed script (`python -m scripts.seed_defaults`) seeds defaults under
`demo@gighound.local` / `demo1234`.

## Run modes

**Docker (recommended):** `./scripts/bootstrap.sh` then
`docker compose up --build` starts Postgres, Redis, the backend (which also
builds and serves the frontend SPA at `http://localhost:8000`), a Celery
worker, and Celery beat. All ports bind to `127.0.0.1`. Requires a `.env`
(created by the bootstrap script); the backend container runs
`alembic upgrade head` on startup. `db` and `redis` carry healthchecks
(`pg_isready` / `redis-cli ping`) and the backend/celery services wait for
them (`depends_on: service_healthy`) and restart `unless-stopped` — first
boot no longer races Postgres initialization.

**Optional local LLM:** the Ollama service sits behind the `llm` profile —
`docker compose --profile llm up -d`, then
`docker compose exec ollama ollama pull qwen3:4b`. Without it (or without
`OLLAMA_BASE_URL` pointing at an external Ollama), generation falls back to
the deterministic offline composer.

**Local development:** run `db`/`redis` from compose, then the backend from
a venv (`uvicorn app.main:app --reload` in `backend/`) and the Vite dev
server (`npm run dev` in `frontend/`, port 5173, `VITE_API_URL` pointing at
the backend). Alternatively `npm run build` once and let the backend serve
`frontend/dist` — the same path Docker uses.

| Variable | Default | Notes |
|---|---|---|
| `GIGHOUND_FRONTEND_DIST` | `<repo>/frontend/dist`; `/app/frontend/dist` in the image | Directory the backend serves as the SPA (StaticFiles with `index.html` fallback; `/api/*` and `/ws/*` are never shadowed). The mount is skipped when the directory does not exist. |

## Stealth worker

The external browser worker pool has its own env surface
(`GIGHOUND_API_URL`, `WORKER_PLATFORMS`, `WORKER_PROXY_<PLATFORM>`,
`WORKER_HEADLESS`, `WORKER_SESSION_DIR`, poll pacing, fingerprint,
`WORKER_ALLOW_SUBMIT`, …) — see **worker/README.md** for the full table and
safety model.

## Rate governor (Phase 4.5)

Platform adapters pace outbound traffic through a rate limiter shared per
`(platform, principal)` (jittered ±30%) and honor optional per-day action
budgets. One "action" = a platform search or a submission/bid. Budgets are
Redis counters (`rl:{platform}:{principal}:{YYYY-MM-DD}`, 48h TTL); when
Redis is down the budget check is a graceful no-op (pacing still applies).

| Variable | Default | Notes |
|---|---|---|
| `GIGHOUND_DAILY_CAP_<PLATFORM>` | unset (unlimited) | `<PLATFORM>` is the uppercase platform name, e.g. `GIGHOUND_DAILY_CAP_LINKEDIN=30`, `GIGHOUND_DAILY_CAP_UPWORK`, `GIGHOUND_DAILY_CAP_FREELANCER`. Actions beyond the cap raise `DailyBudgetExceeded` until the next UTC day. |


## Platform adapters

| Variable | Notes |
|---|---|
| `LINKEDIN_PROVIDER` | `theirstack` (default) or `brightdata` |
| `THEIRSTACK_API_KEY` / `BRIGHTDATA_API_KEY` | LinkedIn job-data providers |

## Credential enrollment (Accounts UI)

Platform credentials live in the Fernet vault (`GIGHOUND_VAULT_KEY` above),
enrolled per platform account via `POST /api/accounts/{id}/credentials` —
see `docs/api-contract.md` (Addendum v6) for the exact contract.

| Variable | Default | Notes |
|---|---|---|
| `FREELANCER_CLIENT_ID` | — | Freelancer OAuth app id. When unset, `GET /api/accounts/{id}/oauth/freelancer/start` returns **501** and users enroll tokens manually instead. |
| `FREELANCER_CLIENT_SECRET` | — | Freelancer OAuth app secret (used by the token exchange/refresh). |
| `FREELANCER_REDIRECT_URI` | `http://localhost:5173/oauth/freelancer/callback` | OAuth callback used in the authorize URL and the code exchange; must match the Freelancer app config. |

## Digest email (optional)

`SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO`. Without `SMTP_HOST`, digests are generated but only logged.

## Frontend

`VITE_API_URL` — backend base URL, default `http://localhost:8000`.
