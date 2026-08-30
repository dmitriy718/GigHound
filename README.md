# GigHound

Multi-tenant SaaS for freelance job discovery and proposal automation:
GigHound watches freelance platforms for matching jobs, drafts tailored
proposals with a local (or hosted) LLM, and puts them in a review queue —
**a human always approves before anything is submitted**. Outcomes and
replies are synced back so win rates per platform, template, and bid band
are learned over time.

## Features

- **Scheduled discovery** — Celery beats run each user's search profiles
  (boolean queries, keyword groups, quality filters) against Freelancer
  (official API), Upwork (official GraphQL), and LinkedIn (via licensed
  data providers; discovery only).
- **AI proposal drafting** — job analysis + draft generation via Ollama
  (local, default) or any OpenAI-compatible provider, with a deterministic
  offline fallback when no LLM is reachable. Rate-card and portfolio aware;
  prompt-injection hardened (job content is delimited and treated as data).
- **Human-in-the-loop queue** — drafts land in `pending_review`; approve,
  edit (with version history), bulk-approve, or reject with reasons that
  feed back into generation (`prompt_hints`). No auto-submit, ever.
- **Submission with audit trail** — Freelancer bids via the API; Upwork via
  the compliant Agency Plus hybrid path (audited handoff to the agency
  manager's browser session). Every action writes an immutable audit row.
- **Learning loop** — outcome/reply sync (Freelancer), template provenance
  and stats, win-rate funnel analytics (queued → approved → submitted →
  replied → hired) per platform/template/bid band.
- **Gig management** — Fiverr-style gig templates, metrics, competitor
  snapshots (via the optional stealth-browser worker).
- **Multi-tenant** — JWT auth, per-user data isolation, per-user encrypted
  credential vault (Fernet), per-(platform, principal) rate governor with
  daily action budgets.

## Architecture

```
                        ┌──────────────────────────────┐
 Browser (React SPA) ──▶│  FastAPI backend             │
   served by backend    │  - REST /api/*  - WS /ws/*   │
                        │  - serves frontend/dist      │
                        └───┬──────────┬──────────┬────┘
                            │          │          │
                     SQLAlchemy    Celery      Redis pub/sub
                            │      broker      (WS fan-out)
                        ┌───▼───┐  ┌──▼──────────────┐  ┌────────┐
                        │Postgres│  │ Redis            │  │ Ollama │ (opt-in
                        │(Alembic│  │ cache + budgets  │  │  LLM   │  profile)
                        │managed)│  └──▼──────────────┘  └────────┘
                        └───────┘   Celery worker + beat
                                      discovery / generation /
                                      outcome sync / digests /
                                      retention
                                            │
                              ┌─────────────▼──────────────┐
                              │ Platform adapters           │
                              │ freelancer · upwork ·       │
                              │ linkedin (read-only)        │
                              │ + stealth-browser worker    │
                              │   (claim/complete protocol) │
                              └────────────────────────────┘
```

Stack: FastAPI + SQLAlchemy + Alembic + Celery + Redis + Postgres ·
React + Vite + TypeScript · Ollama (OpenAI-compatible) or hosted LLM.

## Quickstart (Docker)

Prereqs: Docker with the Compose plugin.

```bash
./scripts/bootstrap.sh            # creates .env from .env.example and fills
                                  # POSTGRES_PASSWORD, GIGHOUND_SECRET_KEY,
                                  # GIGHOUND_VAULT_KEY, GIGHOUND_WORKER_TOKEN
                                  # (idempotent — never overwrites real values)

docker compose up --build -d          # db, redis, backend, celery-worker, celery-beat
docker compose exec backend python -m scripts.seed_defaults   # demo data (optional)
```

Then open <http://localhost:8000> — the backend container runs
`alembic upgrade head` on startup, builds the frontend bundle, and serves
the SPA. Demo login after seeding: **`demo@gighound.local` / `demo1234`**.

Optional local LLM (without it, drafts use the offline composer):

```bash
docker compose --profile llm up -d
docker compose exec ollama ollama pull qwen3:4b
```

### First-run flow

1. Register (or log in as the demo user).
2. Create a keyword group + search filter + search profile (the seed
   provides working defaults) and connect platform accounts/credentials.
3. Wait for the discovery beat (or trigger a search) — qualifying jobs are
   ingested and proposals drafted automatically.
4. Review the proposal queue: edit/approve/reject. Approved Freelancer
   items submit via the API; Upwork items hand off to the agency-manager
   browser session.
5. Watch the analytics view as outcomes and replies sync back.

## Development

```bash
# backend
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
docker compose up -d db redis
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m pytest -q          # test suite (SQLite, no services needed)

# frontend (dev server on :5173 proxies nothing — set VITE_API_URL)
cd frontend
npm ci
npm run dev
npx tsc -b && npx vite build           # type-check + production build
```

CI (`.github/workflows/ci.yml`) runs pytest, `tsc -b` + `vite build`, and
`pip-audit` on every push/PR.

## Configuration

All configuration is env-driven — see **[docs/environment.md](docs/environment.md)**
for the full reference and run modes. Key variables:

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | Required by `docker-compose.yml` |
| `DATABASE_URL` / `REDIS_URL` | Infra connections |
| `GIGHOUND_SECRET_KEY` | JWT signing secret (mandatory outside dev) |
| `GIGHOUND_VAULT_KEY` | Fernet key for the credential vault (mandatory outside dev) |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Local LLM endpoint/model |
| `GIGHOUND_DAILY_CAP_<PLATFORM>` | Per-platform daily action budget |
| `GIGHOUND_DEV_NOAUTH=1` | Dev-only auth bypass — never in production |

## Security notes

- **HITL boundary is hard**: proposals only submit from an approved queue
  item; the Upwork adapter requires `approved_by`; there is no free-text
  submission endpoint. This is a deliberate product invariant, not a config.
- **Auth**: every endpoint except `/api/health`, `/api/auth/register` and
  `/api/auth/login` requires a Bearer JWT; the WebSocket authenticates via
  `?token=` before `accept()`. The user menu offers password change and
  password-verified account deletion (cascades to all tenant data).
- **Credential vault**: platform credentials are Fernet-encrypted at rest,
  per-user scoped; without `GIGHOUND_VAULT_KEY` the vault fails fast.
- **Broker hardening**: Celery accepts JSON only (no pickle); Postgres and
  Redis ports bind to `127.0.0.1`.
- **Anti-ban posture**: shared per-(platform, principal) pacing with jitter,
  daily action budgets, circuit breakers; LinkedIn/Indeed are
  discovery-only by design.

## Documentation

- [docs/environment.md](docs/environment.md) — env vars & run modes
- [docs/api-contract.md](docs/api-contract.md) — API contract
- [docs/platform-intelligence-report.md](docs/platform-intelligence-report.md) — platform risk research
- [evolution/](evolution/README.md) — audits, architecture decisions
  (multi-tenancy, auth, stealth worker), and the phased plan of attack
