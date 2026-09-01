# Review Round 2 — Pass 2a: Coupling & Domino-Effect Audit (backend + worker + async pipeline)

Date: 2026-08-31 · Method: traced every cross-boundary connection to both ends (Celery contracts, DB model coupling, backend↔worker HTTP contract, shared literals, env vars, failure propagation, tenant isolation). Every claim verified by reading code on both sides.

## Findings

**1. [CRITICAL] `scrape_proposal_status` double-completion crashes the worker process**
- Worker posts results, backend marks task `done`: `worker/handlers/proposal_status.py:113-114` → `backend/app/routers/gigs.py:394-399`.
- Runner then unconditionally completes the same task: `worker/runner.py:56` (outside any try/except).
- Backend rejects: `gigs.py:325-326` (409) → `ClaimConflictError` (`worker/client.py:62-63`).
- Nothing catches it: `poll_once` only guards `poll_tasks` (`runner.py:63-66`); main loop `runner.py:92` has no try/except. Worker exits; `docker-compose.worker.yml` has **no `restart:` policy**.
- Domino: any status scrape matching ≥1 proposal (the normal case) kills the worker; all platforms stop being served.
- Fix: tolerate `ClaimConflictError` on the success-path `complete_task`, or have `/proposal-status` leave completion to the worker.

**2. [HIGH] Claimed-never-completed stealth tasks orphaned forever — no reaper**
- Claim flips `pending → claimed` atomically (`gigs.py:303-307`). Nothing resets `claimed` after a worker crash/timeout.
- Domino A: crashed worker wedges every `submit_upwork_proposal` task it held → `queued_for_browser` forever; outcome sync (`outcome_sync.py:25`) and follow-ups (`follow_up.py:41`) watch only `submitted`.
- Domino B: `proposal_status_sync._open_status_task` treats `claimed` as in-flight (`proposal_status_sync.py:53-62`) — one orphaned scrape task permanently suppresses all future status scrapes for that tenant+platform.
- Retention only deletes `done`/`failed` (`tasks.py:472-478`).
- Fix: beat task returning `claimed` tasks older than N min to `pending`, capped re-claim count.

**3. [HIGH] Two Upwork submit paths, divergent status contract — stuck `approved`, duplicate submissions possible**
- Path A (`routers/proposals.py:375-490`) sets `item.status = "queued_for_browser"` at :483.
- Path B (`routers/adapters.py:149-196`) enqueues the identical stealth task (`:180-190`) but **never touches `item.status`**.
- Completion hook only flips items in `queued_for_browser` (`gigs.py:356`).
- Domino: via path B the worker really submits, item stays `approved` — invisible to outcome sync and status scrapes, and **submittable again** → duplicate Upwork submissions.
- Fix: set `queued_for_browser` in adapters.py, or collapse path B into path A.

**4. [HIGH] Upwork browser-session enrollment impossible via API — Upwork stealth path can't be seeded in-product**
- Session endpoint supports upwork (`gigs.py:243-275`); worker consumes it (`worker/browser.py:200-221`); submission handler needs it (`worker/handlers/upwork_proposal.py:31`).
- But enrollment routes upwork to OAuth keys only: `credentials.py:36` `_OAUTH_PLATFORMS = ("freelancer", "upwork")` vs `:37` `_STEALTH_PLATFORMS = ("fiverr", "peopleperhour", "guru", "linkedin", "indeed")` — `storage_state_json` for upwork rejected 422 (`:74-79`).
- Domino: only way to seed Upwork session is CLI `worker/login.py` (headed browser, `--user-id` default 1) — single-tenant, ops-only. Conversely linkedin/indeed enrollment is *accepted* but no worker serves them (`worker/config.py:12`).
- Fix: allow both OAuth and storage_state for upwork; drop linkedin/indeed from stealth set.

**5. [HIGH] Circuit breaker failure counting platform-global — one tenant's broken account halts all tenants**
- Failure count filters platform only, no `user_id` (`gigs.py:335-339`); trips `circuit_breaker.open_circuit(task.platform, …)` (:341).
- Breaker key global: `circuit_breaker.py:22` (`circuit:{platform}`); honored at every enqueue (`stealth.py:49-54`, `fiverr_monitor.py:68-70,222-230`, `gig_analytics.py:90-98`).
- Domino: tenant A's expired session fails 3 tasks/hour → circuit opens → tenant B's fetches/submissions all `skipped_circuit_open` silently. Same pattern: gig-draft cap `gigdraft:{platform}` is platform-global (`fiverr_monitor.py:71`).
- Fix: per-(platform, user) failure counting; platform-global only for manual opens.

**6. [HIGH] Dead handoffs: `submit_fiverr_offer` / `submit_proposal` / `scrape_competitors` never enqueued by backend**
- Defined both sides (`stealth.py:14,17-18`, `worker/handlers/__init__.py:30-38`); zero backend producers.
- UI path promises the handoff: `proposals.py:467-472` returns 501 "leave this item approved and it will be picked up there" — nothing picks it up. Buyer-request offers (`fiverr_monitor.py:200-209`) can be approved but never submitted.
- Competitor intel dead end-to-end: handler exists (`worker/handlers/scrape.py:38-62`), inbox endpoint exists (`gigs.py:189-211`), no producer mints `scrape_competitors` tasks.
- Fix: wire producers (beat turning approved fiverr/PPH/guru items into stealth tasks; weekly competitor tick) or remove dead endpoints + misleading 501 text.

**7. [MEDIUM] "Task succeeded" conflated with "proposal submitted" (latent status-integrity bug)**
- Manual-assist handlers return `submitted: False` when `WORKER_ALLOW_SUBMIT` off (`worker/handlers/manual_assist.py:50-60`); runner reports `success=True` (`runner.py:56`).
- Backend flips to `submitted` on success alone (`gigs.py:357`). Latent today only because of #6.
- Fix: `_apply_submission_outcome` checks `task.result.get("submitted", True)` before flipping.

**8. [MEDIUM] Legacy `task_type` aliases are the LIVE contract, comments say deprecated**
- All 4 production enqueue sites emit legacy strings: `fiverr_monitor.py:78` (`fiverr_create_gig`), `:105` (`upwork_catalog_upsert`), `:225,231` (`fiverr_fetch_buyer_requests`), `gig_analytics.py:93` (`gig_scrape_metrics`).
- Worker resolves only via `LEGACY_ALIASES` (`worker/handlers/__init__.py:21-28`); `models.py:422-423` + `stealth.py:27` call them pre-AD-4.
- Domino: anyone "cleaning up" aliases silently breaks buyer-request monitoring, metrics scrapes, gig drafts — completed as `no handler` failures, feeding #5's global breaker.
- Fix: emit canonical kinds at the 4 sites (one line each), then retire aliases deliberately.

**9. [MEDIUM] Redis cache client never reconnects; mid-run Redis outage escapes as 500s**
- `cache._r` fixed at import (`cache.py:31-38,62`) — Redis down at boot = permanently degraded until restart; per-process `_local` breakers diverge (`circuit_breaker.py:17,52`).
- `get_json`/`set_json`/`invalidate_prefix` don't catch runtime errors (`cache.py:40-59`). Redis drops after boot → `ConnectionError` into request path: `routers/filters.py:71,84`, `circuit_breaker.get_state`, `adapters/ratelimit.py:122` (500 instead of the docstring's "graceful no-op", `ratelimit.py:11-12`).
- Fix: try/except with fallback + reconnect (ws_manager does it right — `ws_manager.py:77-86`).

**10. [MEDIUM] Extended backend outage kills the worker (unretried transport errors)**
- `worker/client.py:54-56` re-raises raw `httpx.TransportError` after 3 tries (~7s); `poll_once` catches only `BackendError`/`ClaimConflictError` (`runner.py:63-66`).
- Domino: backend down >~10s → worker exits; no `restart:`; `depends_on` has no health condition.
- Fix: catch `TransportError` in `poll_once`; add `restart: unless-stopped`.

**11. [MEDIUM] Worker token is a cross-tenant master key; `/stealth-session` leaks full session cookies for any `user_id`**
- `GET /api/gigs/stealth-session?platform=X&user_id=Y` returns complete Playwright storage_state with no binding to a claimed task (`gigs.py:243-275`; auth is just `get_worker`).
- Worker-post endpoints accept arbitrary `user_id`/`gig_id` from body (`gigs.py:171-174,204-211,232-238`). No audit rows.
- Domino: one leaked `GIGHOUND_WORKER_TOKEN` = read/write every tenant + account-takeover-grade cookies.
- Fix: bind stealth-session to an active claimed task; audit-log worker posts; document rotation.

**12. [MEDIUM] Buyer-request fetch ships empty payload → scrapes nonexistent URL, fails silently to zero**
- Backend enqueues `payload={}` (`fiverr_monitor.py:231`); worker defaults `username="me"` (`worker/handlers/buyer_requests.py:18-19`) → `https://www.fiverr.com/users/me/briefs`.
- Domino: 404/redirect; `extract_cards` → [] (`worker/handlers/base.py:61-81`); backend reports `queued: 0` — monitor looks healthy, never yields anything, forever.
- Fix: seller username into `PlatformAccount.settings` + payload; treat empty scrape on valid session as failure signal.

**13. [MEDIUM] Two beat ticks lack the per-tenant isolation every other tick has**
- `fiverr_buyer_request_tick` (`tasks.py:98-106`) and `gig_analytics_tick` (`:112-119`) loop users with no try/except.
- Domino: one tenant's DB error aborts the sweep; later tenants silently miss the cycle.
- Fix: wrap per-user body like other ticks.

**14. [MEDIUM] Digest "sent" count lies when SMTP unconfigured; SMTP vars missing from `.env.example`**
- `send_user_digest` ignores `send_digest_email`'s return (`digest.py:54-61`); `False` at `:79-81` when SMTP_HOST unset.
- Fix: propagate boolean; fail loud when digest_mode != off but SMTP unconfigured; add vars to `.env.example`.

**15. [MEDIUM] celery-worker/beat containers skip migrations — fresh-deploy schema race**
- Backend image migrates in CMD (`backend/Dockerfile`), but compose overrides command for celery services; they depend only on db/redis health.
- Fix: `alembic upgrade head` in worker/beat entrypoint (idempotent) or a one-shot migrate service they depend on.

**16. [LOW] Generation-retry burns a retry when broker is down**
- `tasks.py:343-347`: counter committed before `.delay()`; delay failure consumes the attempt. Two broker blips → permanently stranded `generation_failed`.
- Fix: increment after successful enqueue.

**17. [LOW] No retry/DLQ policy on any Celery task**
- Bare `@celery_app.task` everywhere — no `autoretry_for`, `acks_late`, DLQ. Positives verified: JSON-only serializers (`tasks.py:31-35`); all beat schedule names match registry; every `.delay()` call site matches signature.

**18. [LOW] Free-string `Gig.platform` mints stealth tasks no worker can serve**
- `register_gig` accepts any platform string (`gigs.py:144-157`); `enqueue_metrics_scrape` turns each distinct value into a stealth task (`gig_analytics.py:87-99`); worker serves 4 platforms. Others pend forever.
- Fix: validate platform against the `Platform` literal.

**19. [LOW] Platform-name drift across five hand-maintained lists**
- `schemas.py:7` Literal (includes `indeed`, supported nowhere); `credentials.py:37`; `proposal_status_sync.py:33` `BROWSER_SYNC_PLATFORMS` (duplicates `worker/config.py:12`, currently in sync); `discovery.py:27` `DISCOVERY_PLATFORMS`. Adding a platform = 5 edits, missing one fails silently.
- Fix: single source of truth or asserted-equal test.

**20. [LOW] Minor verified items**
- `proposals.py:46-47,89` batch-loads `Job` by id without tenant filter — safe only via ownership-derived ids.
- Frontend handles a WS `digest` message type (`useAlertsSocket.ts:100`) the backend never broadcasts — dead branch. All other WS types match both ways.
- `digest_mode` window map (`digest.py:26`) treats unknown mode as 24h (Literal-validated today).

## Schema-less JSON couplings (watchlist)
- `StealthTask.payload`: produced in 5 backend sites, consumed in 8 worker handlers, zero validation either side.
- `ProposalQueueItem.submission_result` keys written/read by 7 modules (`generation_retries`, `interview_prep`, `parent_proposal_id`, `bidder_id`/`on_behalf_of`/`connects_required`, `channel`/`response`/`bid_id`, `warning`).
- `AdapterState["pending_submissions"]` shared by `upwork_agency.py:226-235` and `gigs.py:363-367`.
- `GigTemplate.template_json` keys read by `fiverr_monitor.py:131-138` and cross-process by `worker/handlers/gig_draft.py:38-51`.
- Migrations: linear Alembic chain through `c4e2a81f05b7` covers all current columns; no `create_all` at startup; autogenerate-drift not CI-checked.

## Coupling map — 10 most load-bearing connections (by blast radius)
1. **`GIGHOUND_WORKER_TOKEN` ↔ all `/api/gigs/*` worker endpoints** — one shared secret gates cross-tenant reads/writes + session-cookie export.
2. **`stealth_tasks` row contract** — 5 producers ↔ 8 handlers over schemaless JSON; 4-state machine, no reaper.
3. **`ProposalQueueItem.status` state machine** — mutated by 8 modules; two writers already disagree.
4. **Redis `cache._r` singleton** — breaker, LLM bucket, daily caps, pacing locks, ingest limiter, preview cache share one import-time client.
5. **Celery broker + beat schedule ↔ `tasks.py` registry** — rename breaks scheduling silently; no retry/DLQ.
6. **Platform-global circuit breaker** — worker failure reports → global open → every tenant skips.
7. **`submission_result` JSON blob** — implicit API between submission, outcome-sync, retry, follow-up.
8. **Vault `(platform, principal)` convention ↔ `/stealth-session` ↔ worker seeding** — already disagree (finding 4).
9. **Ollama `generateText` ↔ `generation_failed` ↔ retry beat** — well-contained but dependent on blob keys + retry race.
10. **WS event-type literals ↔ frontend union** — 6 types in sync; pub/sub degrades to process-local silently when Redis down.

**Verified clean:** Celery arg names/counts at every `.delay()`; all 7 worker HTTP calls match endpoints/methods/auth/fields both directions; user-facing tenant scoping consistent; worker-facing mutations re-check `item.user_id == task.user_id`; Alembic chain linear and complete.
