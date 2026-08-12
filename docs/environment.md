# GigHound — Environment Variables

All configuration is env-driven; nothing is hardcoded in application logic.
Copy `.env.example` or export these before starting the backend.

## Text generation (Ollama / LLM)

| Variable | Default | Notes |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://ollama:11434/v1` in Docker, `http://192.168.1.68:11434/v1` on LAN | OpenAI-compatible Ollama endpoint. Docker is auto-detected via `/.dockerenv` (or force with `GIGHOUND_IN_DOCKER=1`). |
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
| `REDIS_URL` | `redis://localhost:6379/0` | Cache, rate buckets, circuit breaker, openings pool. Graceful no-op fallback when down. |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated. |
| `GIGHOUND_VAULT_KEY` | ephemeral | Fernet key for the credential vault. **Set this in production** or stored credentials won't survive restarts. Legacy alias `GIGHUNTER_VAULT_KEY` accepted. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

## Platform adapters

| Variable | Notes |
|---|---|
| `LINKEDIN_PROVIDER` | `theirstack` (default) or `brightdata` |
| `THEIRSTACK_API_KEY` / `BRIGHTDATA_API_KEY` | LinkedIn job-data providers |

## Digest email (optional)

`SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO`. Without `SMTP_HOST`, digests are generated but only logged.

## Frontend

`VITE_API_URL` — backend base URL, default `http://localhost:8000`.
