# Implementation Log

Running log of all evolution work. Newest entries on top. Each entry: what changed, where, why,
and verification results. Reports/audits live in their own files (see README.md).

---

## 2026-08-29 — Final audit gaps: digest fan-out, worker .dockerignore, Fiverr/PPH/Guru outcome sync

**1. Digest beat fan-in → fan-out (Scalability).** `run_due_digests` looped all
digest-subscribed users doing a blocking SMTP send inside one Celery task.
Restructured to the established fan-out pattern (`backend/app/digest.py`,
`backend/app/tasks.py`): `digest_tick_core` is now a dispatcher that enqueues
`digest_user_task(user_id)` per due user (due-logic unchanged — hourly always,
daily only at 07:00 UTC — factored into `due_digest_user_ids`); per-user work
lives in `digest_user_core` → `send_user_digest`, with per-user failure
isolation (an SMTP failure returns `{sent: 0, error}` instead of killing the
beat for everyone).

**2. worker/.dockerignore (Runability).** The worker build context is
`./worker` and the Dockerfile does `COPY .` — added `worker/.dockerignore`
(root-file conventions) excluding `.git`, `.venv`, `.sessions`,
`**/__pycache__`, `**/.pytest_cache` so local state never lands in the image.

**3. Fiverr/PeoplePerHour/Guru outcome sync (Advantage).** The learning loop
now auto-closes for all four browser platforms, not just upwork:
- `worker/platforms.py`: read-only sync page configs for fiverr (seller
  inbox, incl. brief responses), peopleperhour (WorkStream list), guru
  (quotes list) — `proposals_url`/`proposal_card`/`proposal_card_fields`/
  `proposal_unread`, best-effort selectors documented as such, same style as
  the upwork config.
- `worker/handlers/proposal_status.py`: platform comes from the task (no
  longer hardcoded upwork); link matching generalized from `a[href*='/jobs/']`
  to all card hrefs; canonical status keywords extended (won→hired,
  lost→declined).
- `backend/app/proposal_status_sync.py`: `enqueue_upwork_status_scrape` →
  `enqueue_platform_status_scrapes` — one `scrape_proposal_status` task per
  browser platform with an enabled account AND submitted/queued_for_browser
  items; in-flight dedupe is now per tenant+platform. Result application was
  already platform-agnostic (per-row tenancy, idempotency) — verified for
  fiverr. Beat task name `upwork_outcome_tick` kept stable so the schedule
  doesn't break; `upwork_outcome_user_core` now returns
  `{enqueued: n, task_ids: [...]}`.

**Tests:** backend 217/217 (+3: digest fan-out/isolation, multi-platform
enqueue, fiverr result application; the old digest test rewritten for the
dispatcher), worker 52/52 (+7: config validity per platform, per-platform
extraction with mocked browser, new status keywords),
`docker compose -f docker-compose.yml -f docker-compose.worker.yml config -q`
OK. Docs: api-contract beat notes generalized, worker/README updated.

---

## 2026-08-29 — Final backend/worker wave (Re-audit #1 gaps: W2–W4)

**1. Upwork outcome sync via worker (Advantage gap #2 closed).**
- New read-only stealth kind `scrape_proposal_status` (`backend/app/stealth.py`,
  worker registry `worker/handlers/__init__.py`).
- `backend/app/proposal_status_sync.py`: `enqueue_upwork_status_scrape` (enabled
  upwork account + open upwork items in submitted/queued_for_browser; max one
  task in flight per tenant) and `apply_proposal_status_results` (hired→hired,
  declined→rejected via `record_outcome`; unread reply → `client_replied_at` +
  `client_replied` WS event; per-row tenancy; idempotent).
- Beat `upwork_outcome_tick` every 60 min, fan-out per active user
  (`upwork_outcome_user_task`).
- `POST /api/gigs/proposal-status` (worker token): applies results and
  completes the task; 401/404/422 guards.
- Worker: `worker/handlers/proposal_status.py` (proposals-page scrape, raw →
  canonical status mapping pending/viewed/interviewing/hired/declined, unread
  badge detection, job matching by external id / URL tail), selectors in
  `worker/platforms.py` (best-effort, documented), `client.post_proposal_status`.

**2. Follow-up due automation (pull → push).** `backend/app/follow_up.py` +
daily 09:05 UTC beat `follow_up_due_tick` (fan-out per user). Gating:
request_type=job, status=submitted only (queued_for_browser excluded — upwork
items flip to submitted on worker confirmation), outcome=pending, no client
reply, submission proxy coalesce(reviewed_at, created_at) > 5 days, no
existing follow_up child (any status — no re-nagging). Cap 5/user/run;
per-item LLM failure → skip+log. Queues pending_review + `proposal_queued` WS
event + AuditLog (`auto: true`). Proxy documented: no submitted_at column —
reviewed_at (approval, last step before submit) stands in.

**3. `GET /api/analytics/trend?weeks=1..26`** (default 8): SQL day-buckets
(func.date + GROUP BY) folded into ISO weeks (`%G-W%V`), oldest first;
submitted (coalesce reviewed_at/created_at), replied (client_replied_at),
hired + win_rate (0–100, null without outcomes) from a new
`submission_result.outcome_recorded_at` stamp written once by
`record_outcome` (legacy rows fall back to created_at). No schema change.

**4. bid_advice refresh:** `GET /api/proposals` recomputes `compute_bid_advice`
from the job's current proposals_count for returned-page items older than
24h; persists only on change.

**5. Compose first-boot race:** healthchecks on db (`pg_isready`) and redis
(`redis-cli ping`); backend/celery-worker/celery-beat `depends_on:
service_healthy` + `restart: unless-stopped`. `docker compose config -q`
validates.

**6. CI:** new `worker` job (pip install worker/requirements.txt, pytest
worker/tests); backend job gains postgres:16 + redis:7 services and a second
pytest pass with DATABASE_URL/REDIS_URL pointed at them (SQLite pass kept —
endpoint tests override the DB dep, the services pass exercises config/
SessionLocal/Redis paths against the real thing).

**7. `scripts/bootstrap.sh`:** set -euo pipefail; creates .env from
.env.example, fills GIGHOUND_SECRET_KEY / GIGHOUND_VAULT_KEY (Fernet format,
stdlib base64url of 32 random bytes) / GIGHOUND_WORKER_TOKEN /
POSTGRES_PASSWORD (openssl rand, python3 fallback); idempotent (existing
real values never touched; the shipped `gighound` placeholder IS replaced);
prints next steps. Referenced from README quickstart + docs/environment.md.

**Contract:** Addendum v7 (trend endpoint, proposal-status endpoint,
bid_advice refresh, outcome_recorded_at stamp, both automations).

**Tests:** +8 backend (`test_wave2.py`: tick enqueue/gating, status mapping
incl. idempotent repost + template no-double-count + cross-tenant skip,
follow-up gating/cap/no-re-nag, trend shape/values/bounds/tenancy, bid_advice
age-gated refresh) → **210/210 green**. +17 worker
(`test_proposal_status.py`: canonical status matrix, match/extract/post,
card-text fallback, empty-items no-op, registry, client post) → **45/45
green**. Beat schedule + task names re-verified; new SQL rendered against
the Postgres dialect (no local PG/Redis/Docker — the CI services pass itself
is unverified locally).

**Deviations:** (a) trend hired/win_rate needs an outcome timestamp — added
as a JSON stamp (`outcome_recorded_at`) instead of a new column/migration;
(b) bootstrap replaces the `POSTGRES_PASSWORD=gighound` example placeholder
(only that exact shipped default) — all other existing values are sacred;
(c) CI Postgres pass could not be executed locally (no server/docker);
workflow YAML validated, PG-dialect SQL rendering checked.

---

## 2026-08-29 — Stage A: backend multi-tenancy + auth + Alembic

**Scope:** plan items 0.1 (auth — upgraded to full multi-tenant per AD-1/AD-2), 4.2 (Alembic),
4.6 (tenancy decision executed). Delegated to coder subagent, verified by parent.

**Added:**
- `backend/app/auth.py` — bcrypt (passlib), JWT HS256 12h tokens, `get_current_user`,
  `GIGHOUND_DEV_NOAUTH=1` escape hatch, `validate_auth_config()` fail-fast, tenancy helpers
  `scoped()`/`get_owned()`.
- `backend/app/routers/auth.py` — register (honors `GIGHOUND_ALLOW_REGISTRATION`), login
  (Redis rate-limited 5/5min per email+IP), me, logout.
- Alembic baseline migration (`backend/alembic/`) — full schema incl. tenancy; zero-drift
  autogenerate check. `create_all` removed; lifespan handler replaces deprecated on_event.
- `users` table + non-null indexed `user_id` FK on all 20 tenant tables; UniqueConstraints
  `Job(user_id,platform,external_id)`, `AdapterCredential(user_id,platform,principal)`,
  `AdapterState(user_id,platform,key)`, `AlertSettings(user_id)`.
- Per-user WS fan-out (`ws_manager` keyed by user_id; `/ws/alerts` requires `?token=`).
- All routers + internals (orchestrator, templates, fiverr_monitor, gig_analytics,
  proposal_gen, vault/StateStore, Celery beats) tenant-scoped; beats iterate active users.
- Seed script seeds demo user `demo@gighound.local` / `demo1234`.
- Tests: `conftest.py` (test secret), `test_auth.py` (9 tests: hashing, JWT, register/login/me,
  per-user isolation, WS auth). Suite: **83/83 green** (74 baseline + 9 new).
- Deps: `passlib[bcrypt]`, `PyJWT`, `alembic`, `bcrypt==4.0.*` (pinned: passlib 1.7.4 breaks on
  bcrypt ≥4.1). `.env.example` + `docs/environment.md` updated with new env vars.

**Deviations:** bcrypt pin (documented in requirements.txt); `keywords` and `agency_audit_log`
tables intentionally without user_id (child/global-adjacent); `backend/.venv` created
(gitignored) since project had none.

---

## 2026-08-29 — Stage B: frontend auth + Phase 0: security hardening (complete)

**Stage B (frontend auth, plan 0.1 frontend half / AD-2).** New `views/Login.tsx` (sign-in /
create-account, dev-credential hint only in DEV); `api/client.ts` token helpers +
Bearer injection + `setOnUnauthorized` 401 flow; `App.tsx` auth gate + header user menu +
logout; `useAlertsSocket` connects with `?token=` and reconnects on token change; `types.ts`
`User`; styles for auth card + user menu. Verified: `tsc -b` clean, `npm run build` ok.
Caveat: contract-matched, no live browser click-through yet.

**Phase 0 remainder (backend, plan 0.2–0.7).**
- 0.2: adapter write endpoints (`/adapters/freelancer/bid`, `/adapters/upwork/proposals`) now
  take only `proposal_queue_item_id`, require caller-owned `approved` item, send exactly the
  queued text/bid, write AuditLog. Free-text `approved_by` bypass eliminated.
- 0.3: submit success writes `proposal_submitted` AuditLog + upwork adapter `close()` in
  finally; `bulk_approve` writes versions + AuditLog + templates per item and skips
  `needs_review=True`; `revert` resets to `pending_review` when content changes. Latent bug
  found & fixed: single-approve AuditLog was missing user_id (non-null FK).
- 0.4: vault key mandatory outside dev; dev key persisted to gitignored `backend/.vault-dev-key`
  (0600, race-safe); `InvalidToken` → clean `AdapterAuthError("re-enroll credentials")`.
- 0.5: compose Postgres password from env (fails fast), ports bound to 127.0.0.1, Celery
  json-only serializers, `load_dotenv()` in config.py.
- 0.6: typed ingest body (422), http(s)-only URL validation, 30 req/min per-user ingest rate
  limit, boolquery length(1000)/depth(32) caps.
- 0.7: `<job_posting>` delimiters + untrusted-data system instruction in both prompts;
  `_strip_prompt_leakage()` output filter on all drafts (strips rate-line/prompt-internal
  leakage, forces needs_review + warning; such items can't be bulk-approved). Offline
  freelancer composer no longer interpolates the rate line (uniform leakage signal).
- Tests: 19 new (`test_phase0.py`) → suite **102/102 green**.

---

## 2026-08-29 — Phase 1: unbreak the existing product (complete)

**Backend (items 1.2–1.7, 1.6-endpoint).**
- Submission unbroken: `submit_proposal` + approval-bound adapter endpoints fall back to
  `PlatformAccount.settings.bidder_id` / `settings.on_behalf_of` (new JSON column, migration
  `6d06a7fdf7dd`); missing → 400 pointing at Accounts page. Upwork success → new truthful
  status `queued_for_browser` (not "submitted"); dedupe gate + status lists updated.
- Pitch templates real: legacy dead generator deleted from orchestrator.py; canonical
  double-brace `{{token}}` dialect everywhere; `proposal_gen.render_pitch_template` renders the
  user's ProfileTemplate through the offline path (unknown tokens → empty string, never raw
  braces); profiles/proposals generation prompts aligned to the same dialect.
- Filters gate the pipeline: `_matching_profiles` + `_passes_profile_filters` — a boolean-match
  profile with `filter_id` must pass `job_matches_filter`; `is_duplicate` jobs no longer get
  drafted; platform kill switch (`platform_enabled` honoring `enabled`/`mode="disabled"`,
  no row = allowed) enforced on search/bid/submit/queue.
- `POST /api/jobs/score-preview` — pure scoring, zero persistence (ScoringConfig dry-run).
- Word boundaries: precompiled `\b` regexes for complexity terms (scoring + proposal_gen
  heuristics), negative keywords ("php" ∌ "graphPHP"), and boolean single-word terms (quoted
  phrases keep substring semantics).
- Tests: `test_phase1.py` 19 new → suite **120/120 green**.

**Frontend (items 1.1, 1.2-UI, 1.6-UI, 1.8, 1.9).**
- GigManager: 4 confirmed mismatches fixed (`is_active`, suggestions `{area,message}`,
  snapshot `created_at`, `queued_tasks.length`); reusable `ErrorBoundary` wraps each tab + view.
- Accounts: per-platform `bidder_id`/`on_behalf_of` fields → `settings`; ProposalQueue:
  `queued_for_browser` badge/banner, Upwork "Queue for browser worker", manual-submission
  platforms get disabled submit + tooltip.
- ScoringConfig playground → dry-run `score-preview` (no more `test-*` rows in production feed).
- WS event-loss fixed: `useNewAlertMessages` hook consumes the messages array with a last-seen
  ref (batched bursts deliver every event); JobFeed/ProposalQueue/BuyerRequestInbox migrated.
- BuyerRequestInbox: server-side `?status=pending_review`, Reject action with reasons, live
  reload on WS events.
- Verified: `tsc -b` clean, `npm run build` ok.

---

## 2026-08-29 — Phase 2: learning loop closed (complete)

**Backend (2.1–2.5, 2.7, 2.8, AD-6).**
- Scheduled discovery (`app/discovery.py`): beat every 15 min (8,23,38,53) per active user per
  enabled SearchProfile — boolean-AST positive-term extraction, keyword-group fallback,
  platform pick via linked filter ∩ enabled accounts, Redis `SET NX EX 900` pacing lock,
  adapter auth errors skip-not-crash. `POST /api/search-profiles/{id}/run-now` (bypasses pacing
  intentionally).
- Generation off request path (`app/ingest.py` + `PipelineContext`): rate card / portfolio /
  profiles / ASTs / filters preloaded ONCE per ingest (N+1 storm killed); ingest enqueues
  `generate_proposal_task` (LLM work no longer in the HTTP path); thin Celery wrappers over
  directly-callable cores.
- prompt_hints wired: `REVIEWER FEEDBACK TO INCORPORATE` section injected outside the untrusted
  `<job_posting>` tags.
- Outcome + reply sync (`app/outcome_sync.py`, 30-min beat): Freelancer `get_bid_status` →
  hired/rejected via `record_outcome`; `get_threads` → `client_replied_at` + `client_replied`
  WS event. Idempotent, per-item error isolation.
- Template provenance: `template_for_approval` reuses + increments when reviewer started from a
  template; mints only when `save_as_template` (new column) and no template_id; `uses` counted
  at selection; tautological filter removed.
- Safe automations: digest beat (hourly + daily 07:00 UTC), generation_failed retry (<24h,
  max 2), auto-archive (deadline passed or >14 days, daily 03:17 UTC), `POST
  /api/jobs/bulk-archive`.
- Dedupe hardened: same user/platform, ≤72h, cap 500 (was: last-200 cross-platform scan per
  job).
- AD-6: WS fan-out over Redis pub/sub (`gighound:ws:{user_id}`) with local fallback; subscriber
  lifecycle in app lifespan — Celery tasks can now broadcast.
- Analytics: `GET /api/analytics/funnel` (funnel, by_platform, by_template, bid bands,
  rejection reasons; tenant-scoped).
- Migration `0adef05255f0` (save_as_template, client_replied_at). Contract Addendum v4.
- Tests: +22 (`test_phase2.py`) → suite **142/142 green**. One existing test updated for the
  intentional off-path-generation behavior change.

**Frontend (2.6, run-now, bulk archive, client_replied).**
- New `views/Analytics.tsx` — funnel bars, per-platform/bid-band tables, template leaderboard
  (null win rates sink with "no outcomes yet"), rejection chips, onboarding empty state; nav
  under Sell cluster.
- SearchProfiles "Run search now" per profile; JobFeed bulk select + archive; ProposalQueue
  "client replied" badge + toast + WS refresh; AlertsPanel renders client_replied.
- Verified: `tsc -b` clean, build ok.

---

## 2026-08-29 — Phase 3: winning-advantage features (complete)

**Backend (3.1–3.4).**
- Client intelligence (`app/client_intel.py`): adapters now populate `client_info.client_id`/
  `name`/`past_hires`/`identity_verified` (Upwork totalHires + client_id; Freelancer owner id /
  identity_verified / username) — the previously unreachable scoring points now live. Client
  identity fallback chain client_id → name → (country, rating, spend-bucket). `GET
  /api/jobs/{id}` gains `client_history` (past bids per client: hired/rejected/ghosted); a
  `CLIENT HISTORY:` section feeds generation (outside untrusted tags).
- Follow-ups: `POST /api/proposals/{id}/follow-up` — gated (submitted-ish, outcome pending, no
  pending sibling), new platform-tuned follow-up system prompt + offline composer, humanize +
  leak filter, new `request_type="follow_up"`, AuditLog.
- Interview prep: `GET /api/proposals/{id}/interview-prep` — LLM JSON or deterministic
  5-question offline fallback grounded in portfolio/missing_info; cached on the item.
- Bid-market intelligence: `bid_advice` (bid/caution/skip + reason) computed at queue time from
  proposals_count × quality_score; won-bid rate learning (`app/rate_learning.py` — ≥3 samples →
  50% pull toward winning average, clamped ±20%, fixed-price only).
- Migration `7c1e9a2b4d38` (bid_advice column). Contract Addendum v5. +13 tests → **155/155**.

**Frontend.** ProposalQueue: Draft-follow-up action + follow_up badge/parent note, interview
prep panel (collapsible Q&A, pain points/talking points chips, cached per item), bid_advice
badges + filter. JobFeed drawer: Client history section (fetches detail endpoint; colored by
outcome mix). Verified: tsc clean, build ok.

---

## 2026-08-29 — Phase 4: runability & sellability (complete)

- Docker: `backend/Dockerfile` (multi-stage, repo-root context — builds frontend dist, py3.12,
  non-root, `alembic upgrade head && uvicorn`); `frontend/Dockerfile`+nginx.conf (optional
  standalone); compose gains backend / celery-worker / celery-beat / ollama (profile `llm`),
  all ports localhost-bound; SPA served by backend via `SPAStaticFiles` (verified live via
  TestClient: /, SPA fallback, API precedence).
- Hardcoded LAN Ollama IP removed from the repo (default now localhost; set OLLAMA_BASE_URL
  explicitly otherwise).
- CI: `.github/workflows/ci.yml` (backend pytest, frontend tsc+build, pip-audit with a
  documented starlette ignore list pending a fastapi major refresh); pytest split into
  `requirements-dev.txt`; unused `websockets` dep removed; `cryptography` 43→50 (Fernet
  round-trip verified), `python-dotenv` 1.*.
- Root `README.md`: product story, architecture diagram, Docker quickstart, demo creds, dev
  workflow, security notes.
- Rate governor (4.5): shared limiter registry per (platform, principal) — cross-request pacing
  actually works now; ±30% jitter; per-platform daily action budgets
  (`GIGHOUND_DAILY_CAP_<PLATFORM>`, Redis counter, no-op when down); principal threaded through
  all adapters. 9 new tests.
- Fixed pre-existing blocker: joined-line syntax error at schemas.py:408.
- Deviation (documented): Redis-down daily-budget behavior is graceful no-op (task spec)
  rather than hard pause (plan text) — pacing still applies.
- Verified: 171 pytest green ×5 runs, tsc clean, vite build ok, `docker compose config`
  validates, pip-audit clean. Docker images NOT built (daemon permission); needs a real build
  test where daemon access exists.

## 2026-08-29 — Stealth-browser worker (complete, AD-4 / plan 4.7)

**Backend support:** `POST /api/gigs/stealth-tasks/{id}/claim` (atomic claim, 409 on race;
`claimed_by`/`claimed_at` columns, migration `b3f1c9d42e10`); `GIGHOUND_WORKER_TOKEN` auth
(`get_worker` strict / `get_worker_or_user` dual) — worker poll is cross-tenant by design,
mutations worker-token only; complete endpoint flips `queued_for_browser` → submitted/failed
(HITL loop closed); windowed circuit-breaker counting (3 failures/hour, replaces lifetime);
canonical task-kind registry + `enqueue_stealth_task` (circuit-aware); upwork agency handoff
now ALSO creates a StealthTask row (previously nothing for a worker to poll); result-posting
endpoints dual-auth with explicit user_id tenancy. +8 tests.

**Worker package (`worker/`, Python + Playwright/Chromium):** config (env-driven, per-platform
proxy, session dir, headless, allow-submit gate); resilient API client (backoff, 409-aware);
persistent browser contexts per (platform, user) with UA rotation, human typing consuming the
backend `typing_plan`, challenge/CAPTCHA detection → `{captcha: true}` escalation; per-platform
selector config centralized in `worker/platforms.py`; handlers: metrics/competitor scraping,
buyer-request (Briefs) fetch, upwork agency proposal submit (the approved action, with
screenshots + challenge escalation), gig draft creation (DRAFT only, never publish),
manual-assist mode for submit_proposal/submit_fiverr_offer (fills, screenshots, no final click
unless WORKER_ALLOW_SUBMIT=1); runner (poll 45s±15s, claim-before-execute, exactly-one-complete,
crash-safe); `python -m worker.login` headed session seeder; README (setup, safety model,
selector maintenance). 24 tests (browser-free) + a REAL Chromium smoke test (typing plan,
challenge detection, JSON-LD, session persistence). Compose override
`docker-compose.worker.yml` (depends_on backend — reconciled by parent after parallel-edit
race).

**Not verified (honest list):** handlers against live Fiverr/Upwork/PPH/Guru (selectors are
best-effort starting points by design), Docker image builds (no daemon access), end-to-end
worker↔backend loop against a running stack.

**Totals at this checkpoint: backend 171 tests, worker 24 tests, frontend tsc+build clean.**

---

## 2026-08-29 — Re-audit #1 (verification + scorecard)

Two independent read-only audits against the evolved codebase (suites re-run: backend 171,
worker 24, tsc clean; migration chain + beat/task names + worker protocol verified).

**All 12 previously-confirmed bugs re-verified FIXED.** New bugs found (N1–N7):
- N1: `routers/proposals.py:278` — `user` var shadowing → 500 on template save (save=true).
- N2: template provenance half-wired — `template_id`/`save_as_template` unreachable from UI;
  approvals still always mint templates.
- N3: bulk-approve returns id lists; frontend types counts → broken toast.
- N4: `bidder_id` not persisted in submission_result → own-message filter in outcome sync inert
  (false client_replied risk).
- N5: Fiverr daily offer counter global across tenants (missing user_id in Redis key).
- N6: `skills/suggest` unauthenticated. N7: template `uses` triple-counted.

**Scorecard:** Scalability 6 · Sellability 4 · Runability 8 · Ease of use 6 · Advantage 7.
Cross-cutting gaps: (1) credential enrollment impossible via product (shell-only vault seeding)
— caps Sellability/Ease; (2) outcome/reply auto-sync is Freelancer-only — caps Advantage;
(3) beat ticks fan-in not fan-out; (4) no onboarding/attention surface; (5) compose first-boot
race (no healthchecks/restart); (6) analytics in-memory, no time dimension; (7) follow-ups
pull-only. Fix waves W1–W4 planned below.

---

## 2026-08-29 — Re-audit #1 fixes: N1–N7 bugs + scalability gaps (backend)

**Bug fixes.**
- N1: `user` prompt-var shadowing fixed in `routers/proposals.py` (`generate_proposal_template`)
  and `routers/profiles.py` (`generate_profile_template`) — save=true no longer 500s
  (regression test `test_generate_template_save_true_persists`).
- N2 (backend half): `ProposalReviewAction` gains `template_id` (validated tenant-owned, 404
  otherwise) and `save_as_template: bool = True` — persisted on the item per approval, so
  `template_for_approval` reuses the picked template / skips minting. Bulk approve unchanged.
  Note: the approve call is now the source of truth for the flag (body default True
  overrides a stored False — creation-time flag semantics superseded; test updated).
- N4: freelancer submit persists `bidder_id` into `submission_result` (outcome_sync's
  own-message filter now has its input).
- N5: fiverr daily offer counter key is per-tenant (`fiverr:offers:{user_id}:{day}`);
  `offers_remaining_today(user_id)` threaded through monitor + gigs router.
- N6: `GET /api/skills/suggest` now requires auth.
- N7: template `uses` counted at SELECTION only (`top_templates`); increments removed from
  `template_for_approval` and `record_outcome` (test asserting old semantics updated).

**win_rate scale unified to 0–100** (analytics funnel by_platform/by_template/by_bid_band;
was 0–1). Contract doc updated.

**Scalability fixes.**
- Fan-out beats: `discovery_tick_core` is now a dispatcher enqueuing
  `discover_profile_task(user_id, profile_id)` per (active user, profile);
  `outcome_sync_tick_core` enqueues `outcome_sync_user_task(user_id)`. Cores stay directly
  callable; per-pair/per-user failure isolation preserved.
- Analytics funnel is pure SQL aggregation (GROUP BY platform + conditional sums, CASE bid
  bands, GROUP BY rejection_reason) — no load-all in Python. Response shape unchanged.
- `GET /api/proposals` N+1 killed (single IN query for the page's jobs) and is now paginated:
  `limit` (default 50, max 200) + `offset`, response ALWAYS `{items, total}` (contract change —
  frontend updated in parallel).
- `Job.client_key` (indexed, nullable, migration `c4e2a81f05b7`) derived from
  `client_info`+`platform` via a before_insert/before_update listener
  (`client_intel.client_key_for`) — covers all ingest paths; no backfill (NULL = no history).
  `client_history_for_job` is now a keyed GROUP BY join instead of a full-table Python scan.
- LLM-inline endpoints (follow-up, interview-prep, template generation): `db.commit()` before
  the LLM await releases the pooled connection for the duration of the call (the session is
  otherwise held from `get_current_user` onward). Chosen over a 45s timeout — no behavior
  change, connection pool unblocked.

**Tests:** +11 new (`test_reaudit1.py`) + 4 updated → suite **202/202 green**.
Migration chain verified (upgrade head on fresh sqlite; client_key column + index present).

---

## 2026-08-29 — Final polish wave: account lifecycle, data retention, docs sweep

**Account lifecycle (auth gaps closed).**
- `POST /api/auth/password` (`routers/auth.py`, schema `PasswordChangeIn`): verifies the
  current password (400 on mismatch), enforces min 8 / max 72 on the new one (422), updates
  the hash. Stateless JWTs stay valid — the session continues, new password applies at next
  login.
- `DELETE /api/auth/account` (schema `AccountDeleteIn`): verifies the password (400 on
  mismatch), then deletes the user row. Verified every tenant table's `user_id` FK is
  `ON DELETE CASCADE` in both `models.py` (`_user_fk()`) and the initial migration
  (24 CASCADE constraints), so the row delete wipes all tenant data — no explicit per-table
  deletion needed. **Choice noted:** no final `AuditLog` row is written — `audit_log.user_id`
  is a non-nullable cascading FK, so the row would be erased by the very delete it records;
  an application-log line (`account deleted: user_id=… email=…`) is emitted instead.
- Frontend (`components/AccountModals.tsx`, wired into the `App.tsx` user menu): "Change
  password" modal (current + new + confirm) and "Delete account" modal with typed-confirm
  (account email) + password; successful deletion drops the session to Login. New client
  calls `changePassword` / `deleteMyAccount` (renamed to avoid collision with the existing
  platform-account `deleteAccount`).

**Data retention.** New daily beat `retention_tick` (04:11 UTC, `tasks.py`), per tenant:
archived jobs fetched >90d ago are hard-deleted EXCEPT any still referenced by the proposal
queue (skipped + counted — archived jobs normally have none); done/failed stealth_tasks
completed (fallback: created) >30d ago deleted; audit_log rows >365d deleted. Returns/logs
`{jobs_deleted, jobs_skipped_referenced, stealth_tasks_deleted, audit_log_deleted}`.
Boundaries tested at ±1 day per cutoff (racy-free), plus tenant isolation of the skip check.

**Scorecard nits.**
- (a) `GET /api/skills/suggest` verified already authed (fixed in the re-audit wave, N6).
- (b) README + `docs/environment.md` consistency sweep: public-by-design endpoint list
  corrected (`/api/health`, `/api/auth/register`, `/api/auth/login` — the new auth endpoints
  are JWT-protected); password/account flows documented; retention added to the beat
  architecture line; stealth-worker env pointer added.
- (c) env reconcile: `CACHE_TTL_SECONDS` (read in `config.py`) was undocumented — added to
  `docs/environment.md`. All other `os.environ`/`getenv` reads across `backend/app` + `worker`
  were already covered by `.env.example`, `docs/environment.md`, or `worker/README.md`.

**Final sweep.** Route audit: 101 routes carry an auth dependency (`get_current_user` /
`get_worker` / `get_worker_or_user`); public by design: `GET /api/health`,
`POST /api/auth/register`, `POST /api/auth/login` (+ FastAPI's built-in `/openapi.json`,
`/docs`, `/redoc`). `/ws/alerts` authenticates via `?token=` before accept (not visible to
the HTTP-route audit).

**Tests:** +4 new (`test_account_lifecycle.py`, `test_retention.py`; the deletion test
enables SQLite's FK pragma so the cascade is genuinely exercised) → backend suite
**214/214 green**, worker 45/45, frontend `tsc -b` + `vite build` clean,
`docker compose config -q` OK. Docs: api-contract Addendum v8.

---

## 2026-08-29 — Close-out: final scorecard 10/10/10/10/10

Three independent post-implementation audits (verification, pillar scorecard, final) drove six
fix waves. Final audit before W6: 9/10/9/10/9 — the three 9s were capped by exactly three
items: digest beat fan-in (last fan-in beat), missing `worker/.dockerignore`, and manual
outcome entry for Fiverr/PPH/Guru. W6 closed all three (see entry above). Final verification
run by parent: backend 217/217, worker 52/52, tsc clean, vite build ok, compose config valid.
Documented with justifications and honest environmental limits in `evolution/final-scorecard.md`.

---

## 2026-08-30 — End-to-end smoke test (native fallback path)

Docker daemon still inaccessible (compose CLI present, no daemon; no Postgres/Redis services,
no sudo), so the E2E was run against the documented dev-fallback path: SQLite + Redis-down
graceful degradation, real secrets from the bootstrap-generated `.env`, auth enforced.

**Verified live over HTTP (uvicorn on 127.0.0.1:8899):**
- Full Alembic chain (7 migrations) applied clean on a fresh SQLite DB; seed idempotent.
- Auth: login → JWT → /api/auth/me; unauthenticated requests → 401.
- Ingest: real job ingested + scored; **filter gating confirmed live** (job without posted_at
  correctly blocked from auto-queue by the seeded 48h filter — the fix from Phase 1 working as
  designed); `javascript:` URL → 422; red-flag scoring via score-preview (score 0 + flags).
- Generation: `generate_proposal_core` ran the full pipeline with Ollama unreachable →
  graceful heuristic analysis + offline composer → queued pending_review item with confidence,
  bid, and correct bid_advice.
- Review: approve → approved; submit without bidder_id → clean actionable 400 (not a crash).
- Analytics: funnel + trend endpoints return correct tenant-scoped shapes.
- Credentials: enroll → 204, status shows key names only (no value leak).
- Worker protocol: stealth task created → worker-token poll (cross-tenant) → claim → re-claim
  race 409 → claim without token 401 → complete → done; stealth-session endpoint returns
  storage_state correctly.
- SPA: backend serves frontend/dist at / with fallback routes → 200; API routes take
  precedence.
- Redis-down behavior: cache disabled warning, WS subscriber retry loop, everything functional.

**Issue found & fixed:** seeded pitch-template tokens (`{{technical_approach}}`,
`{{milestone_breakdown}}`, `{{availability}}`, `{{skill_area}}`, `{{requirement_1/2}}`,
`{{experience}}`, `{{years}}`) rendered as empty strings → awkward drafts. Extended
`render_pitch_template` (proposal_gen.py) to support all seeded tokens; one test expectation
updated for the improved `{{deliverable}}` fallback ("the core deliverable"). Suite: **217/217
green** after the fix.

**Still unverified (unchanged, environmental):** Docker image builds, live-platform selectors,
worker↔backend loop against a live stack, real LLM output quality.

---

## 2026-08-30 — CI check + real-LLM generation test + output-quality fixes

**CI:** the pushed commit's CI run failed all 4 jobs in seconds — NOT a code issue: GitHub
Actions refused to start them ("account is locked due to a billing issue"). Workflow validates
locally; runs will execute once the account's billing is resolved. (User action required on
github.com billing settings.)

**Real-LLM test (LAN Ollama, qwen3:4b, live HTTP path):** ingest → score (62.6) → analysis →
draft. The pipeline genuinely works with a real model: correct skill/deliverable extraction,
platform-tuned draft, one clarifying question, word cap respected, gap-honesty visible
("No NestJS backend experience on my end, but…"). Two real output-quality issues found & fixed:
1. **Bid above client max:** calculate_bid's cap only triggered at >1.2× budget_max and the
   won-bid nudge ran after it → bid $6,300 on a $4–6k job. Now: hard cap at budget_max × 0.98,
   applied after the nudge (verified live: $5,880 with transparent rationale).
2. **Personality marker misfire:** inject_personality prepended "Funny enough," to a
   subordinate clause ("Funny enough, since your WebSocket endpoints…") — the exact
   security-theater behavior pass2 flagged. Now: only first-person declarative mid-text
   sentences are eligible; no eligible sentence → text untouched. Verified live: marker lands
   naturally ("Here's the thing — my portfolio includes…").
Suite: **217/217 green**.

---

## 2026-08-30 — Docker images built + full-stack E2E (the last environmental gap closed)

**Docker access:** user added dima to the docker group; agent session predates the change, so
all Docker commands run through a `newgrp docker` wrapper (group applies per-process).

**Builds (first ever, both green):**
- `gighound-backend` multi-stage (Node frontend build → py3.12 runtime, non-root, migrations on
  start) — backend, celery-worker, celery-beat images.
- `gighound-worker` (py3.12 + Playwright + Chromium headless shell) — ~2 min download, clean.

**Full stack live:** db (healthy) + redis (healthy) + backend + celery-worker + celery-beat +
stealth worker — all up via compose with healthchecks/service_healthy working as designed.
- Backend ran all 7 migrations on container start; SPA served at :8000 (200).
- Seed inside container exposed a real bug: `python scripts/seed_defaults.py` failed with
  ModuleNotFoundError (Python puts the script's dir, not cwd, on sys.path). Fixed: seed script
  self-inserts its parent dir into sys.path; image rebuilt; verified ("All collections already
  populated" — idempotent, no error).
- **First fully-autonomous pipeline run as designed:** HTTP ingest → Redis broker → Celery
  worker → real LAN Ollama (qwen3:4b) → pending_review proposal (conf 72.7, bid $4,250 under
  the $5k cap, personalized draft). No human touched anything between ingest and queue.
- Stealth worker container live: authenticates with its token, polls all four platforms
  (fiverr/upwork/peopleperhour/guru) at 200s, no tasks pending (correct — nothing enqueued).

**Remaining unverified (unchanged):** live-platform selectors (need real accounts), GitHub CI
(account billing lock — user action).


---

## 2026-09-01 — Phase 0 of review-round-2 plan implemented and verified ("stop the bleeding")

Basis: `evolution/review-2026-08-31/implementation-plan.md` (user approved Phase 0). All 18
work items implemented in 6 clusters, each committed separately; master `1089519`..`ec61c84`.

**Cluster A — worker liveness (P0-1, P0-2).** `worker/runner.py`: success-path `complete_task`
treats `ClaimConflictError` as benign (task already finalized server-side); `httpx.TransportError`
tolerated in poll loop, per-task processing, and failure reports; one bad task can no longer kill
the sweep. Compose: worker gets `restart: unless-stopped`, `shm_size: '1gb'`, /proc-based
healthcheck; db/redis get restart policies; backend image CMD `exec`s uvicorn (SIGTERM works);
image HEALTHCHECK on `/api/health` (the real route — `/health` was the SPA fallback); celery
services opt out of the HTTP healthcheck. Worker 57 tests.

**Cluster B — stealth-task lifecycle (P0-3..P0-5).** New `reclaim_count` column (migration
`d3f8a2c91e07`); `stealth_reaper_tick` (5-min beat) returns stale `claimed` tasks to `pending`
(15-min timeout, 3-reclaim cap → failed); retention also purges `skipped_circuit_open`.
adapters.py Upwork submit path now flips items to `queued_for_browser` (status contract unified
with proposals.py — duplicate-submission hole closed). Both submit paths 409 on circuit-open
instead of stranding items; status-sync stops counting skipped tasks as enqueued. Backend 222 tests.

**Cluster C — submit guards & data-loss (P0-6..P0-8).** Submit endpoint transitions
`approved→submitting` via conditional UPDATE before any external call (second submit → 409);
pre-dispatch failures release back to `approved`; `revert_version` refuses terminal/in-flight
statuses; `submitting` added to status enums (backend + frontend). Editors use `||` so empty
`humanized_text` falls through (approve-wipes-text closed); approve rejects empty proposal_text
(422); schema emits `humanized_text: null` when unset. `API_URL` defaults to same-origin `''`;
`wsUrl` derives `ws(s)://host` from `window.location` — prod bundle no longer hardcodes
localhost:8000. Backend 226 tests; tsc + build clean.

**Cluster D — tenancy & fan-out (P0-9..P0-12).** Upwork accepts OAuth OR storage_state
credentials (dual validation; Accounts UI 3-way picker); linkedin/indeed enrollment removed.
Circuit breaker scoped `circuit:{platform}:{user_id}` for auto-trips (global opens still block
everyone); gig-draft cap per-account. Buyer-request tick skips users without an enabled fiverr
account or with an in-flight fetch; both ticks got per-tenant error isolation. Enqueue carries
seller username from account settings (skips when unset); worker fails loudly without it,
absolutizes URLs, stable brief IDs (sha1 fallback), no hardcoded USD. Backend 236, worker 69 tests.

**Cluster E — output integrity (P0-13, P0-16).** `inject_personality` splits with captured
separators and rejoins verbatim — paragraph breaks survive; `strip_ai_tells` gains `,,` and
leading-`.` artifact passes. Negative-keyword jobs archive at ingest regardless of filter
thresholds; generation gate refuses excluded jobs. Backend 241 tests.

**Cluster F — infra & resilience (P0-14,15,17,18).** One-shot `migrate` compose service gates
backend/celery on schema readiness. Digest sent-count reflects actual SMTP delivery; SMTP/DIGEST
vars in `.env.example`. Generation-retry counter increments only after successful enqueue;
fiverr offer cap enforced by atomic INCR (fail-closed when Redis can't enforce); buyer-request
flush IntegrityError-guarded. Cache client: socket timeouts, per-op RedisError containment, lazy
reconnect; breaker/textgen/ratelimit/ingest degrade instead of 500ing. `upwork_catalog_upsert` +
non-fiverr gig drafts no longer emitted (429 "not supported yet" instead of 100%-fail tasks);
`register_gig` validates platform; emitters use canonical task kinds + resolvability test.
Backend 254 tests.

**Verification (all self-run, not delegated):**
- Gates: backend 254/254, worker 69/69, `tsc -b` + `vite build` clean.
- Images rebuilt; full stack up; backend + worker report `(healthy)` via the NEW healthchecks.
- `migrate` service ran `c4e2a81f05b7 → d3f8a2c91e07`; `reclaim_count` confirmed in DB.
- Reaper smoke: seeded a 20-min-stale claimed task → `stealth_reaper_tick_core()` returned
  `{'reclaimed': [184]}` with "reclaim 1/3" logged. Worker then picked it up and completed it
  (200) WITHOUT the process dying — the original crash-on-success path exercised live.
- Redis-outage smoke: `docker compose stop redis` → `GET /api/jobs` 200, `POST /api/jobs/ingest`
  200 (previously 500 after commit); restart → 200, lazy recovery confirmed; smoke rows cleaned.
- Worker loop alive throughout, polling all 4 platforms.

**Phase 0 status: COMPLETE (18/18).** Next: Phase 1 (security hardening) on user approval.


---

## 2026-09-01 — Phase 1 implemented: security hardening (10/10)

Basis: `evolution/review-2026-08-31/implementation-plan.md` Phase 1. Implemented in 4 clusters;
master `a8294b3`..`097bec9`. Suites: backend 257→284, worker 69→72, tsc clean.

**Cluster G — worker-endpoint binding (P1-1, P1-2).** `complete_stealth_task` now requires
`status=claimed` + matching `claimed_by`/`worker_id` (pending backcompat dropped; worker client
sends worker_id). proposal-status posts validate task state. stealth-session requires an active
claimed task for the (platform, user_id) pair + honors `mode=disabled`, and every session read is
audit-logged (never the data). Worker mutation endpoints now write AuditLog rows with worker
identity. **Side fix:** test suite moved to Redis db 15 with per-test flush — the live stack's
pacing locks/breaker keys on db 0 were colliding with test user ids, making discovery tests
timing-dependent (root-caused to beat's `discovery:{user}:{platform}` NX locks; suite now
deterministic — 257/257 across repeated runs with the stack running).

**Cluster H — token hygiene (P1-3, P1-4, P1-10).** `hmac.compare_digest` for the worker token.
JWTs mint a `jti`; logout denylists it (TTL = remaining lifetime); change_password also sets a
per-user not-before → all outstanding tokens die (fail-open when Redis down, documented). WS auth
moved to single-use 30s tickets (`POST /api/alerts/ws-ticket`, atomic GETDEL) with legacy `?token=`
kept as degradation fallback; frontend stops reconnecting on 4401 and routes to logout.
`validate_auth_config` fails fast on missing/malformed vault key in non-dev; rotation documented.
passlib removed → direct bcrypt (`>=4.2,<6`); existing `$2b$` hashes verify natively (compat test).

**Cluster I — IDOR + brute-force + hygiene (P1-5..P1-7).** Search-profile `keyword_group_id`/
`filter_id` ownership-validated (404, no existence leak); discovery treats foreign refs as absent;
`register_gig` validates `template_id`; proposal job loads tenant-scoped. Login limiter counts
failures only (success no longer self-429s), adds per-IP bucket, dummy-verifies unknown emails
(timing oracle closed); register gains per-IP bucket. Generic 502s (crafted messages kept);
security-headers middleware (CSP/nosniff/DENY/referrer, HSTS behind `GIGHOUND_BEHIND_TLS`);
CORS `*` rejected in non-dev; filter preview scan capped at 500; shared per-user LLM-generation
rate limit (20/h, new `backend/app/ratelimit.py`) across all five LLM-cost endpoints.

**Cluster J — secrets surface + injection (P1-8, P1-9).** Worker container no longer receives
the root `.env` (only its own 14 vars via explicit `environment:`); session dirs 0700,
storage_state + screenshots 0600; bootstrap.sh `chmod 600 .env` + portable sed; seed refuses the
demo account when `GIGHOUND_ENV=production`. `<job_posting>` fence tokens neutralized inside
untrusted content at all four prompt-assembly sites; adversarial-fixture tests.

**Phase 1 gate items:** all test-covered (forged worker_id → 409; cross-tenant refs → 404;
post-password-change token rejected; WS ticket single-use; prod seed refuses demo). Stack rebuild
deferred to Phase 2 end (worker-heavy changes land there).


---

## 2026-09-01 — Phase 2 implemented: stealth & ban-risk (8/8)

Basis: plan Phase 2. Implemented in 4 clusters; master `bc96276`..`57923b6`.
Suites: backend 284→292, worker 72→113, tsc clean.

**Cluster K — submission truthfulness (P2-2, P2-3).** Upwork submit is verified against
per-platform success/failure markers; ambiguous post-click outcomes report
`submitted_unverified` (never `failed` — the click happened) so humans reconcile instead of
double-submitting; backend maps `result.submitted` explicitly; manual-assist gate no longer
flips unsubmitted work to `submitted`; new status in analytics (not counted submitted) + UI
("submitted — verify on platform"). `fetch_page` detects login walls/missing logged-in markers
→ `SessionExpiredError` → `session_expired` task failure + `needs_reenrollment` account flag +
audit row. Zero-extraction metrics → `selector_suspect` instead of fabricated zeros.

**Cluster L — isolation & session lifecycle (P2-1, P2-5).** stealth-session carries `proxy_url`
from `PlatformAccount.settings`; worker prefers per-account proxy over platform fallback;
`_parse_proxy` validates scheme/host/port. localStorage seeded ONCE via evaluate (permanent
init-script stomping removed); idle context reaper (`WORKER_CONTEXT_IDLE_SEC=1800`) bounds
Chromium count and makes re-enrollment effective without restart.

**Cluster M — fingerprints & behavior (P2-4, P2-6).** Persistent per-(tenant,platform)
fingerprint bundles built from the ACTUAL bundled Chromium version (UA + Sec-CH-UA +
OS-coherent WebGL + viewport + TZ/locale, overrides via session payload), persisted 0600.
Stealth shim patches webdriver/WebGL/languages/plugins/userAgentData (each guarded).
Per-keystroke jittered cadence (thinking pauses, punctuation hesitation, bursts; typo plan
untouched); scrape flows warm-enter via base_url + simulate reading; `WORKER_ACTIVE_HOURS`
circadian gate keeps scrapes unclaimed off-hours, human-approved submits always run.

**Cluster N — task hygiene & hardening (P2-7, P2-8).** Fresh page per task + `close_pages`
after each; gig drafts require majority-of-fields (floor 3) before Save-as-Draft with missed-
selector reporting; cooperative per-task deadline at the `_sleep` chokepoint
(`WORKER_TASK_TIMEOUT_SEC=600`) — thread-based timeouts empirically impossible with
thread-affine sync Playwright (greenlet.error), documented. pytest split to
`requirements-dev.txt`; image runs as `USER gighound` (browsers at /opt/ms-playwright);
`WORKER_ALLOW_SUBMIT_<PLATFORM>` per-platform gates; SingletonLock live-holder warning.

**Ban-risk scorecard re-score (was → now):**
| Dimension | Was | Now | Basis |
|---|---|---|---|
| Per-tenant IP isolation | 1 | 7 | per-account proxy_url served via task-bound session (needs operator proxies) |
| Fingerprint consistency | 2 | 8 | persistent coherent bundles, real-Chromium UA/CH-UA, geo overrides |
| JS-environment patching | 2 | 7 | webdriver/WebGL/languages/plugins/userAgentData shim |
| Headless concealment | 3 | 6 | shim covers main tells; Sec-CH-UA HTTP headers residual (documented) |
| Behavioral humanization | 4 | 7 | jittered cadence + warm entry + reading simulation |
| Timing/circadian plausibility | 3 | 7 | active-hours gate (per-tenant TZ still future work) |
| Session realism & integrity | 4 | 8 | one-time seeding, expiry detection, re-enrollment surfacing |
| Navigation naturalness | 2 | 6 | warm entry + scrolls; no referrer chains yet |
| Safety rails | 7 | 8 | + per-platform submit gates, task deadlines, reaper |
| Failure-mode truthfulness | 2 | 9 | verified submits, submitted_unverified, no fabricated zeros |

**Overall stealth posture: ~3/10 → ~7/10** (targets met; live-platform selector validation
remains the designated follow-up, as does per-tenant timezone alignment).


---

## 2026-09-01/02 — Phase 3 implemented: frontend UX & contract (8/8)

Basis: plan Phase 3. Implemented in 3 clusters; master `d397b04`..`86d0128`.
Suites: backend 292→297, tsc + build clean. **CI unblocked** (user resolved GitHub billing) —
runs green on master since Cluster O.

**Cluster O — resilience & WS lifecycle (P3-1, P3-2).** App-level ErrorBoundary (reload);
crash vectors removed (`!` assertion, unguarded `analysis.required_skills`); 30s fetch timeout;
debounced min-score slider; stale-response/wrong-drawer races closed with sequence refs.
Backend broadcasts `proposal_status_changed` on worker submission outcomes (post-commit);
views refetch on socket reconnect (`useReconnectRefetch`); WS buffer cleared on logout
(cross-tenant alert leak); dead `digest` WS type removed from hook + docs.

**Cluster P — review-loop preservation (P3-3, P3-4).** Draft edits persist to sessionStorage
per user, restore only when server text still matches the draft base (survives 401 re-login);
approve versions the PREVIOUS text (Revert-to-v1 = the AI draft again); bulk-approve warns on
unsaved edits. JobFeed pager (was capped at 50); buyer requests filter server-side via
`request_type` (200-cap invisibility gone); `POST /api/proposals/{id}/retry-generation`
requeues generation_failed items in place (+ UI button); auto-archive spares jobs with live
queue items.

**Cluster Q — safety & contract truth (P3-5..P3-8).** `window.confirm` on all 7 unconfirmed
deletes; Modal Escape + dirty-guard (3 biggest forms). SPA auto-handles the Freelancer OAuth
callback (localStorage account handoff; paste fallback kept); default redirect now the
all-in-one origin. api-contract.md: Auth + Adapters sections, WS ticket handshake, claim
binding, `request_type`, retry-generation. Split-frontend path made real: nginx `/api`+`/ws`
proxy, `ARG VITE_API_URL=""`, `split-frontend` compose profile. `GigMetric.week` type aligned.
client.ts: header merge can't drop Authorization, Content-Type only with body; toast timers
tracked; archive refreshes stale rows; platform locked on account edit; gig URL validated.

**Ease-of-use re-score (was → now):** JobFeed 5→8 (pager, debounce, race fixes),
ProposalQueue 6→8 (drafts survive, truthful versions, retry path, live status),
BuyerRequestInbox 5→8 (server-side filter, drafts, no crash vector), Accounts 6→8 (OAuth flow,
confirms, locked platform), Analytics 7→8 (reconnect refetch, timeout), Settings/Onboarding
7→8 (confirms, modal safety). Weighted: **~5.5/10 → ~8/10** (target met).


---

## 2026-09-02 — Phase 4 implemented: supply chain & infra (7/7)

Basis: plan Phase 4. Implemented in 2 clusters; master `04ce924`..`0ca4dbe`.
Suites: backend 297→298, worker 113, tsc + build clean, **pip-audit + npm audit: 0 known
vulnerabilities everywhere**.

**Cluster R — runtime upgrades (P4-1, P4-2, P4-6).** fastapi 0.115→0.141.1 / starlette
0.46.2→1.6.0 — all 7 suppressed advisories cleared with ZERO code changes; the
`--ignore-vuln` block deleted (pip-audit is a clean hard gate). node:20-slim (EOL) →
node:22-slim in both Dockerfiles + CI (verified with a real targeted stage build). Google
Fonts CDN → bundled @fontsource imports (GDPR/SPOF closed, fonts in dist). `.env` excluded
from docker build context. celery-beat schedule persisted (named volume + `--schedule`).
Python-version guidance documented (3.12 for CI parity; 3.13+ unblocked now passlib is gone).

**Cluster S — CI & pinning (P4-3, P4-4, P4-5, P4-7).** pip-audit gate extended to worker
requirements; npm audit gate added (vite 5→6 cleared the esbuild advisory — 0 vulns
dev+prod); actions SHA-pinned; pip-audit pinned; timeouts + concurrency; dependabot
(actions/pip/npm/docker — already opened its first PRs). redis → **valkey/valkey:8-alpine**
(BSD drop-in; ships redis-cli compat — healthcheck unchanged, live-verified); postgres
16.15-alpine, python 3.12.14-slim, nginx 1.31.4-alpine, ollama 0.33.2 (was :latest).
uv-compiled hashed `requirements.lock` for backend (50 pkgs) + worker (16); Docker + CI
install `--require-hashes`. New CI jobs: alembic upgrade-head smoke on real Postgres,
docker-build check (all 3 images), router-prefix ↔ api-contract.md drift test.
Full stack rebuilt + booted on the pinned images: all containers healthy.


---

## 2026-09-02 — Phase 5 implemented: product completion (5/5)

Basis: plan Phase 5. Implemented in 2 clusters; master `0641c33`..`91c7f17`.
Suites: backend 309→328, worker 113, tsc + build clean.

**Cluster T — dead handoffs resolved + platform registry (P5-1, P5-2).** Approving a fiverr
buyer_request item now auto-dispatches `submit_fiverr_offer` through the stealth contract
(payload matches the manual-assist handler; per-tenant breaker honored; no fiverr account →
stays `approved` with audit note). New `POST /api/proposals/{id}/mark-submitted`
(approved/failed → submitted, channel + audit row) replaces the misleading 501 with an honest
400 + a manual escape hatch in the UI. `scrape_competitors` marked reserved/unwired (no
user-facing config surface exists — deliberately not invented). New canonical registry
`backend/app/platforms.py` (worker/oauth/stealth/discovery/browser-sync/all; `indeed`
documented as accepted-but-unserved); scattered literals swapped to imports; worker-set
equality test.

**Cluster U — celery robustness + correctness tail (P5-3, P5-4).** autoretry+backoff on all
11 beat ticks (`generate_proposal_task` excluded — own bounded retry path); HALF_OPEN admits
exactly one trial (Redis NX token + per-process fallback); beat-singleton documented.
Correctness tail: hourly/fiverr bids capped at client max; short tags word-boundary matched
("ai" ≠ "available"); own messages can't fire `client_replied`; digest filters inactive users,
`SMTP_TLS` opt-out; ingest threshold comment corrected (code was right); short primary terms
word-boundary; `budget_max=0` respected; atomic template increments + Redis-locked rate
samples; boolquery rejects mid-query garbage; partial unique index
`uq_proposal_queue_live_job` (migration `e4a91b6c2d08`) closes the duplicate-generation race.

**Incidental wins during these phases:** CI unblocked by user — green on master including the
new alembic-smoke/docker-build/contract-drift jobs; the new worker pip-audit gate caught
`python-dotenv` PYSEC-2026-2270 on its first run (fixed same-day, gate proven).

---

## Final five-pillar scorecard (P5-5)

Scored against the verified state at master `91c7f17` (backend 328, worker 113, CI green,
full stack live). Evidence basis: review-round-2 reports + per-phase verification above.

| Pillar | Score | Evidence |
|---|---|---|
| **Scalability** | 9/10 | Multi-tenant with per-tenant breakers/caps/pacing; reaper + retention bound queue growth; hashed reproducible builds; alembic CI smoke; one-beat documented; idempotent ticks with autoretry. −1: single worker per session volume (no horizontal worker HA yet). |
| **Sellability** | 9/10 | 0 known CVEs everywhere (pip+npm), pinned images, BSD-clean stack (valkey), token revocation, audit trail, per-tenant isolation, honest docs (api-contract + drift test), demo gated out of prod. −1: no billing/SSO (AD-7 deferred, documented). |
| **Runability** | 10/10 | bootstrap → compose up on a fresh clone; pinned images + hashed locks; migrate service orders schema; healthchecks + restart policies on every service; deterministic suites (328+113) isolated from live state; CI validates migrations and image builds on every push. |
| **Ease of use** | 8/10 | Per-view re-score (see Phase 3 log): all core views 8/10 — pager, drafts that survive 401s, truthful versions, retry paths, live status updates, confirms on destructive actions, working OAuth. −2: no frontend test suite yet; accessibility basics only. |
| **Advantage (securing gigs)** | 8/10 | Ban-risk 3→7/10 (per-account proxies, coherent persistent fingerprints, stealth shim, humanized cadence, circadian gate); submission truthfulness (verified submits, submitted_unverified, no fabricated zeros); closed loop: dispatch → verified outcome → win-rate learning. −2: live-platform selectors still need first-real-session validation; per-tenant timezone alignment future work. |

**Weighted overall: 8.8/10** — from "simple aid" to a defensible multi-tenant SaaS whose
automation is designed to keep tenant accounts alive while doing verifiably honest work.

**Standing follow-ups (honest register):** live-platform selector validation (needs real
accounts — designated maintenance point in worker/platforms.py); per-tenant timezone
alignment; horizontal worker scaling story; billing/SSO (AD-7); frontend test suite;
dependabot PR triage (it opened several on day one, incl. node 22→26 — review deliberately).
