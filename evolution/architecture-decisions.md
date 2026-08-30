# Architecture Decisions — SaaS & Stealth Worker

**Date:** 2026-08-29 · **Status:** Accepted by user (multi-tenant SaaS + build stealth worker).

## AD-1: Multi-tenancy model

- New `users` table: `id`, `email` (unique, citext-style lower), `password_hash`, `display_name`,
  `created_at`, `is_active`.
- **Every tenant-owned table gets `user_id` (FK users.id, indexed)**: keyword_groups,
  search_filters, jobs, alert_settings (singleton → per-user row), profile_templates,
  portfolio_items, rate_card, search_profiles, platform_accounts, proposal_queue, templates,
  rejection_feedback, gig_templates, gigs, gig_metrics, competitor_snapshots, stealth_tasks,
  audit_log, adapter_credentials, adapter_state.
- Every router query is scoped through a `get_current_user` dependency; there is no code path
  that returns another tenant's rows. Helper: `scoped(db, Model, user)` query factory.
- Shared/global data (skills taxonomy, Fiverr gig taxonomy) stays static, not per-user.
- **Vault scoping**: credential rows carry `user_id`; the Fernet key remains deployment-level
  (envelope/KMS per-user keys deferred — documented risk, see pass2 §2). `credential_ref` values
  are namespaced `vault://{platform}/{principal}` and only resolvable for the owning user.

## AD-2: Authentication & sessions

- Password hashing: **bcrypt via passlib** (new dep `passlib[bcrypt]`).
- Tokens: **JWT** (new dep `PyJWT`), HS256, secret `GIGHOUND_SECRET_KEY` env (fail fast if unset
  outside dev), 12h access tokens. No refresh tokens in v1 (documented trade-off).
- Endpoints: `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`,
  `POST /api/auth/logout` (client-side token discard).
- All existing routers require auth. `GET /api/health` stays public.
- WebSocket `/ws/alerts`: token passed as `?token=` query param (browser WS can't set headers),
  verified before `accept()`.
- Frontend: token in localStorage (`gighound_token`), `Authorization: Bearer` header in
  `api/client.ts`, login/register view, auto-redirect to login on 401.
- Registration policy: v1 = open registration behind optional `GIGHOUND_ALLOW_REGISTRATION`
  (default true for self-hosted; set false for closed SaaS + invite flow later).

## AD-3: Schema management

- **Alembic** introduced now, before the user_id migration. Initial migration = current schema +
  tenancy columns. `create_all` removed from startup; `alembic upgrade head` in entrypoint/docs.
- New deps: `alembic`.

## AD-4: Stealth-browser worker

- New top-level `worker/` package (separate from backend): Python + **Playwright** (Chromium),
  runs as its own process/container.
- Protocol (extends existing StealthTask model):
  - Poll `GET /api/gigs/stealth-tasks?platform=&status=pending` with a worker token
    (`GIGHOUND_WORKER_TOKEN`, separate from user JWT).
  - **Claim before execute**: new `POST /api/gigs/stealth-tasks/{id}/claim` — atomic
    `UPDATE ... WHERE status='pending'` → `claimed` with `claimed_by`/`claimed_at`; prevents
    double-execution (fixes pass1 finding).
  - Complete via existing `POST /api/gigs/stealth-tasks/{id}/complete`.
- Task kinds v1: `fetch_buyer_requests` (re-targeted: Fiverr Briefs surface check), `scrape_gig_metrics`,
  `scrape_competitors`, `create_gig_draft`, `submit_upwork_proposal` (agency handoff),
  `submit_proposal` (PPH/Guru copy-assist), `submit_fiverr_offer`.
- Anti-ban: per-platform session persistence (encrypted storage_state in vault), human-scale
  pacing with jitter (consumes `typing_plan` from antidetect), proxy support via env
  (`GIGHOUND_PROXY_{PLATFORM}`), CAPTCHA/challenge detection → fail task with
  `result.captcha=true` → circuit breaker trips after windowed failures → escalate to human in UI.
- The worker never bypasses HITL: it only executes tasks created from approved queue items.
- Fiverr "Buyer Requests" code re-targeted to **Briefs**/buyer-request-equivalent scraping;
  old monitor kept but pointed at the current surface.

## AD-5: Learning loop closure (Phase 2 anchor)

- Discovery: Celery beats per enabled SearchProfile per user (freelancer/upwork/linkedin).
- Generation: moved off request path into Celery task `generate_proposal_task(job_id)`.
- Outcome sync: beat polls Freelancer `get_bid_status`/`get_threads` per submitted item →
  `outcome` auto-update + `client_replied` WS events.
- Feedback: `prompt_hints` wired into proposal_gen; template provenance (reuse vs mint).
- Analytics: funnel aggregation endpoint + dashboard view.

## AD-6: WS fan-out for multi-process

- WS broadcasts go through **Redis pub/sub** (`gighound:ws` channel); `ws_manager` subscribes and
  fans out to local connections. Works across uvicorn workers and Celery. Fallback to
  process-local when Redis is down (single-process dev).

## AD-7: Non-goals (v1)

- No billing/subscription system yet (Sellability track adds it after onboarding polish).
- No refresh-token rotation, no SSO/OAuth login.
- LinkedIn/Indeed submission automation stays out of scope (discovery only) — Extreme risk per
  platform-intelligence-report.
