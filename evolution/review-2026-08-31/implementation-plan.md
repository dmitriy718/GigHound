# Review Round 2 — Implementation Plan

Date: 2026-08-31 · Status: **FULLY IMPLEMENTED — Phases 0-5 done and verified (2026-09-02); see ../implementation-log.md for per-phase logs + final five-pillar scorecard (8.8/10)** · Basis: 7 audit reports in this folder (pass-1, pass-2a, pass-2b, pass-3a..3d, cross-verification). Every item below was confirmed by direct source re-read and/or independent multi-pass corroboration (see cross-verification.md). Item IDs reference the pass reports.

**Ground rules for implementation:** one commit per work item (or tight cluster); run the full gate after each phase — backend `pytest -q` (currently 217), worker `pytest -q` (currently 52), frontend `tsc -b` + `vite build`; never leave the gate red between phases; update docs when behavior changes.

---

## PHASE 0 — Stop the bleeding: liveness, data-loss, double-submit (est. ~2 days)

These are the bugs that silently kill the pipeline, destroy user work, or can send duplicate/garbage proposals under a tenant's real name.

- **P0-1 Worker crash-on-success & crash-on-blip.** [2a#1, 2b#2, 3c claim1/F1] In `worker/runner.py`: treat `ClaimConflictError` from the success-path `complete_task` (:56) as benign ("already finalized"); wrap `process_task` and the failure-report `complete_task` calls (:44,:50) so `httpx.TransportError` is logged and skipped, never fatal. In `worker/client.py:54-56` keep the retry, but callers must never see the raw raise escape the poll loop. Fix `worker/README.md:22`'s false "loop never dies" claim.
- **P0-2 Restart policy + healthchecks.** [2b#2, 3c#10, 3d#15] `docker-compose.worker.yml`: `restart: unless-stopped`, healthcheck, `shm_size: '1gb'`. `docker-compose.yml`: restart policy for db/redis; `HEALTHCHECK` in backend image; `exec` in the backend CMD so SIGTERM works (backend/Dockerfile:37).
- **P0-3 Stealth-task reaper.** [2a#2, 3b claim3] New beat task: `claimed` tasks older than N minutes (compare `claimed_at`) → back to `pending`, with a `reclaim_count` cap → `failed` after cap. Extend retention (`tasks.py:472-478`) to also purge `skipped_circuit_open` rows.
- **P0-4 Divergent Upwork submit paths.** [2a#3] In `routers/adapters.py:149-196`: set `item.status = "queued_for_browser"` after enqueue (mirroring `proposals.py:483`) — or route path B through the same helper. Prevents stuck-`approved` and duplicate submissions.
- **P0-5 Circuit-open stranding.** [3b N1, N23] After `enqueue_stealth_task`, inspect the returned task: if `skipped_circuit_open`, do NOT flip the item to `queued_for_browser`; return 409 with the circuit reason. Same check in `proposal_status_sync.enqueue_platform_status_scrapes` (:90-93) so skipped tasks aren't counted as enqueued.
- **P0-6 Double real-money submission guards.** [3b N3] (a) `proposals.py:386-387`: make submit a conditional `UPDATE … WHERE id=:id AND status='approved'` before the external call (pattern already exists at `gigs.py:303-313`). (b) `revert_version` (:286-310): refuse when item is in a terminal/flight status (`submitted`, `queued_for_browser`, `hired`, etc.).
- **P0-7 Approve-wipes-text.** [2b#5, 3d#3] `ProposalQueue.tsx:80` + `BuyerRequestInbox.tsx:23`: `??` → `||`. Belt-and-braces: backend schema serializes `humanized_text or None` (schemas.py:323) and `approve` rejects empty `proposal_text` (proposals.py:102-104).
- **P0-8 Same-origin API default.** [2b#1, 3d#1] `frontend/src/api/client.ts:35-36`: `?? ''` (backend already serves SPA+API+WS same-origin). Verify `wsUrl()` handles empty base. Keep `VITE_API_URL` override for split deployments.
- **P0-9 Upwork credential enrollment.** [2a#4, 2b#9] `routers/credentials.py:36-37`: allow `storage_state_json` for upwork (both OAuth and stealth key sets valid); remove linkedin/indeed from `_STEALTH_PLATFORMS` (no worker serves them).
- **P0-10 Platform-global breaker → per-tenant.** [2a#5, 3b N4] Failure counting at `gigs.py:335-339` gains a `user_id` filter; breaker keys become `circuit:{platform}:{user_id}` for auto-trips (platform-global reserved for manual opens). Gig-draft cap key `gigdraft:{platform}` → per-user (`fiverr_monitor.py:71`).
- **P0-11 Buyer-request fan-out gating.** [3b N4] `fiverr_buyer_request_tick` (`tasks.py:98-106`): enqueue only for users with an enabled Fiverr PlatformAccount, and skip when a pending/claimed fetch already exists (reuse the `_open_status_task` dedupe pattern from `proposal_status_sync.py:53-62`). Add the missing per-tenant try/except to this tick and `gig_analytics_tick` (`tasks.py:112-119`) [2a#13].
- **P0-12 Buyer-request payload.** [2a#12, 3b claim5, 3c claim3, #19, 3b N24] Store seller username in `PlatformAccount.settings`; include it in the enqueue payload (`fiverr_monitor.py:225,231`); worker uses it instead of `"me"` (`buyer_requests.py:18-19`); absolutize URLs, stable ID from brief slug, parse currency (no hardcoded USD).
- **P0-13 Humanizer output integrity.** [3b N8, N9] `antidetect.py:176-186`: preserve original separators (split capturing the separator, rejoin with it). `:147-166`: add artifact passes `,\s*,` → `,` and leading `^[.,\s]+` → `""`. Add unit tests with real draft fixtures.
- **P0-14 Migration race + digest truth.** [2a#14, #15, 3d#9] One-shot `migrate` compose service (`alembic upgrade head`) with `depends_on: service_completed_successfully` for backend/celery-worker/celery-beat. `digest.py:54-61`: propagate `send_digest_email`'s boolean; log loud when mode≠off but SMTP unconfigured; add SMTP/DIGEST vars to `.env.example`.
- **P0-15 Retry/counter races.** [2a#16, 3b claim1, N5, N14] `tasks.py:343-347`: increment counter after successful `.delay()`. `fiverr_monitor.py:172-216`: gate offers on atomic INCR return, not peek-then-increment. Guard `process_buyer_requests` flush with IntegrityError catch (`:197-198`, mirror `ingest.py:122-127`).
- **P0-16 Negative-keyword exclusion.** [3b N2] `ingest.py:136-141`: archive jobs whose `score_breakdown` has `excluded_by_negative_keyword` regardless of filter thresholds; add a minimum-quality gate to `generation_gates_pass` (`orchestrator.py:156-193`).
- **P0-17 Redis runtime resilience.** [2a#9, 3b claim6, N11] `cache.py`: socket timeouts on the client, wrap get/set/delete/invalidate in try/except `redis.RedisError` → degrade (and lazy-reconnect). Audit the uncaught call sites: `circuit_breaker.get_state`, `textgen.py:105-108`, `fiverr_monitor.py:31-34`, `adapters/ratelimit.py:122`, `ingest.py:170`.
- **P0-18 Broken task types.** [3c claim5, 2a#18] `upwork_catalog_upsert`: either add the upwork gig-form config to `worker/platforms.py` or stop emitting it (fiverr_monitor.py:105). Fix the `{platform}_create_gig` ternary (fiverr_monitor.py:78) to emit only resolvable types. Validate `Gig.platform` against the `Platform` literal in `register_gig` (`gigs.py:144-157`). Switch the 4 live legacy emitters to canonical task kinds (one line each) [2a#8], then add a backend test asserting backend kinds ⊆ worker handler keys.

**Phase 0 gate:** all suites green + native smoke: kill -9 the worker mid-claim → reaper returns task; submit via both Upwork paths → single submission, correct status; Redis stopped mid-run → API degrades, no 500s.

---

## PHASE 1 — Security hardening (est. ~2 days)

- **P1-1 Worker-endpoint binding.** [3a claim2, F2] `complete_stealth_task` requires `task.claimed_by == body.worker_id` and `status == "claimed"` (drop pending backcompat at `gigs.py:325-326`). `/proposal-status` validates the caller relationship. All worker posts write AuditLog rows with worker identity.
- **P1-2 stealth-session scoping.** [2a#11, 3a claim1] Bind session release to an active `claimed` task for that (platform, user_id); honor `mode != "disabled"` (gigs.py:255-259); audit-log every session read.
- **P1-3 Token hygiene.** [3a F1, F3, F4, #10] `hmac.compare_digest` for the worker token (auth.py:133-134). Redis jti denylist checked in `get_user_from_token`, bumped on password change/logout (kills the 12h stolen-token window). WS: replace `?token=` JWT with a single-use short-lived ticket endpoint; frontend handles 4401 → logout instead of infinite reconnect (`useAlertsSocket.ts:107-116`).
- **P1-4 Vault operability.** [3a F5] `validate_auth_config` also checks `GIGHOUND_VAULT_KEY`; document rotation; (stretch) key-id prefixing.
- **P1-5 IDOR fixes.** [3a F6, claim3b] Ownership-validate `keyword_group_id`/`filter_id` on search-profile create/update (orchestration.py:23-42) and scope the discovery `db.get`s (`discovery.py:60-77`); validate `template_id` in `register_gig`; tenant-filter the `Job` batch loads (`proposals.py:46-47,89`).
- **P1-6 Brute-force & enumeration.** [3a F7] Login: per-IP bucket + per-account bucket (independent of email), dummy bcrypt verify on unknown users (timing oracle), don't increment on success. Register: rate limit; make the 409 non-enumerating or accept + document.
- **P1-7 Response hygiene.** [3a F8, F9, F11] Generic 502 messages (log internals); typed pydantic bodies replacing remaining `body: dict` endpoints; security-headers middleware (CSP, X-Content-Type-Options, X-Frame-Options, HSTS behind TLS flag); startup validation that `CORS_ORIGINS != *` with credentials; paginate filter preview; rate-limit LLM-cost endpoints.
- **P1-8 Secrets surface.** [3c#17, #18, 3d N13, N14] Worker compose gets only its 4 vars via `environment:` (drop root `env_file`). Worker session dirs 0700; document screenshot retention/wipe. `bootstrap.sh`: `chmod 600 .env`, portable sed (or python edit). Seed script: refuse to create the demo account when `GIGHOUND_ENV=production` (or require explicit flag); document closing `GIGHOUND_ALLOW_REGISTRATION` for public deploys.
- **P1-9 Prompt-injection hardening.** [3b N7] Escape/strip the literal `</job_posting>` sequence (and other fence tokens) from untrusted content before interpolation in `proposal_gen.py` (3 sites); keep `_strip_prompt_leakage`; add an adversarial-fixture test.
- **P1-10 passlib → bcrypt/pwdlib.** [P1#4, 3a claim4] Replace passlib with direct `bcrypt` (or `pwdlib`), unpin bcrypt; keep hash/verify behavior identical; test existing hashes still verify.

**Phase 1 gate:** suites green + security smoke: forged worker_id completion rejected; cross-tenant keyword-group reference rejected; stolen-token-after-password-change rejected; WS ticket flow works; demo account absent in prod mode.

---

## PHASE 2 — Stealth & ban-risk: the "securing" differentiator (est. ~3 days)

Product goal: raise the ban-risk scorecard (pass-3c) from ~3/10 toward 8/10. Ordered by ban-impact.

- **P2-1 Per-tenant proxy isolation.** [3c F2] `PlatformAccount.settings["proxy_url"]` per (tenant, platform), served through the (now task-bound) stealth-session response; worker `browser.py:191-193` uses the per-tenant proxy; validate `_parse_proxy` port (`browser.py:112-114`); document proxy provider options. Without this, multi-tenant = mass-linkage ban vector.
- **P2-2 Submission truthfulness.** [3c F3, F4, claim2, 2a#7] Per-platform `submit_success` confirmation selectors in `platforms.py`; after click, verify (toast/URL/"already applied"); ambiguous → `submitted_unverified` (new item status, UI-visible, never auto-retried) instead of `failed`/`submitted`. Backend `_apply_submission_outcome` honors `result["submitted"]` explicitly.
- **P2-3 Session-expiry detection.** [3c F6] Per-platform `logged_in_marker` + login-redirect check in `fetch_page` (`base.py:23-30`); expired → fail with `session_expired` (surfaces in Accounts UI, no breaker trip). Never post zeroed metrics: zero-field extraction → skip + `selector_suspect` flag (scrape.py:27-31).
- **P2-4 Fingerprint coherence.** [3c F7, F8] Pin a fingerprint bundle per (tenant, platform) persisted next to the session dir: UA derived from the actual bundled Chromium build, matching Sec-CH-UA, viewport, timezone/locale aligned to proxy geo. Add the minimal init-script stealth shim: `navigator.webdriver` delete, WebGL UNMASKED_VENDOR/RENDERER spoof, plugins/languages coherence.
- **P2-5 Session seeding fix.** [3c F5, #23, 2a watchlist] localStorage applied once after first navigation (not `add_init_script` forever, `browser.py:137-140`); prefer cookie-only seeding. Context idle-TTL reaper (close contexts unused >30 min, `browser.py:158,197`) — also makes re-enrollment take effect without restart.
- **P2-6 Behavioral realism.** [3c #12, #13] Randomized per-keystroke delays with punctuation pauses/bursts (`browser.py:88-92`); scroll/read pauses in scrape flows; warm-up navigation (dashboard → target) instead of cold `goto`; server-side per-tenant active-hours window that delays non-urgent stealth tasks.
- **P2-7 Task hygiene.** [3c #14, #15, #16] Fresh page per task + close in finally (`browser.py:225`); per-task wall-clock budget with cancellation; gig-draft handler requires ≥N fields filled before Save-as-Draft and reports missed selectors (`gig_draft.py:38-56`, `_fill` :17-23).
- **P2-8 Worker hardening.** [P1#9, 3c claim6, #11, #21] Split `requirements-dev.txt` (pytest out of prod); non-root `USER` in worker Dockerfile; per-platform submit gates instead of global `WORKER_ALLOW_SUBMIT`; per-worker session volume note (or backend-claimed profile lock) to prevent concurrent-profile corruption.

**Phase 2 gate:** worker suites green + manual review of stealth diff + ban-risk scorecard re-scored in the log (target ≥7/10 on isolation/fingerprint/truthfulness dimensions).

---

## PHASE 3 — Frontend UX & contract (est. ~2 days)

- **P3-1 App-wide resilience.** [3d N1, 2b#13] ErrorBoundary around `<main>`; remove the `!` at `BuyerRequestInbox.tsx:73`; guard `item.analysis?.required_skills` (ProposalQueue.tsx:600). `AbortSignal.timeout(30s)` in `request()` (client.ts:66-96) [N8]; abort/sequencing for `load` + debounce the min-score slider (JobFeed.tsx:157-163) [N9].
- **P3-2 WS lifecycle.** [2b#3, #4, #10, 3d N2] Backend broadcasts `proposal_status_changed` from `_apply_submission_outcome` (gigs.py:349-367); frontend handles it. Refetch-on-socket-open in views. `setMessages([])` on logout/token change (useAlertsSocket.ts:29-37). Remove or implement the dead `digest` WS type (docs + hook) [2b#6].
- **P3-3 Review-loop work preservation.** [3d N3, N6, N7] Draft edits persisted to sessionStorage keyed by proposal id (restored after 401 re-login); version history records the *previous* text before overwrite (proposals.py:103,118); bulk-approve warns when selected rows have unsaved edits (or includes drafts).
- **P3-4 Zombie & pagination fixes.** [3d N4, N5, 3b N17] JobFeed pagination using existing `total`; BuyerRequestInbox server-side `request_type` filter instead of fetch-200-and-filter. `generation_failed`: add a "Retry generation" action hitting a requeue endpoint that resets the retry counter (also fixes permanent stranding). Auto-archive excludes jobs with non-terminal queue items (`tasks.py:414-428`) [3b N16].
- **P3-5 Destructive-action & modal safety.** [3d N10, N11] Confirmation dialogs for the 7 unconfirmed deletes; modal dirty-check before backdrop-close; Escape-to-close; basic focus trap.
- **P3-6 OAuth flow.** [2b#14, 3d#7] SPA handles `/oauth/freelancer/callback` (read `?code=`, complete, close); default redirect = serving origin, not :5173.
- **P3-7 Contract truth.** [2b#7, #12] Update `docs/api-contract.md`: Auth section (login/register/me/logout), Adapters section, WS handshake (`?ticket=`, 4401), template-generate endpoints, claim endpoint; fix `GigMetric.week` and `humanized_text` types both sides; remove frontend-null-accepting lies. Decide `frontend/Dockerfile`+`nginx.conf`: wire properly (with `/api`+`/ws` proxy) or delete [2b#11, 3d N21].
- **P3-8 Latent client bugs + papercuts.** [3d N17, N18, N19] Merge headers instead of spread-overwrite in `request()`; no `Content-Type` on GET; toast timer cleanup; archive removes row under active filter; bid-advice filter server-side or label honestly; lock platform on account edit; wrap gig-URL input in a form.

**Phase 3 gate:** `tsc -b` + `vite build` green; manual click-through of the review loop (edit → 401 → re-login → edits survive; submit → status flips live without refresh); ease-of-use scorecard re-scored (target ≥8/10 on JobFeed/ProposalQueue/BuyerRequestInbox).

---

## PHASE 4 — Supply chain & infra (est. ~1.5 days)

- **P4-1 fastapi upgrade.** [P1#1] `fastapi==0.141.*` (starlette ≥1.3.1 clears all 7 suppressed advisories); run full suite; delete the `--ignore-vuln` block in ci.yml:98-107.
- **P4-2 Node LTS.** [P1#2] `node:22-slim` (or 24) in backend/Dockerfile:7, frontend/Dockerfile:9, ci.yml:68.
- **P4-3 Audit gating everywhere.** [P1#6, 3d N22] CI: pip-audit for `worker/requirements.txt`; `npm audit --omit=dev` gate; upgrade vite off 5.4.21 (esbuild GHSA-67mh-4wv8-2f99); add `timeout-minutes` + `concurrency` to jobs; pin actions to SHAs; pin pip-audit itself [P1#8].
- **P4-4 Image pinning.** [P1#5, #10] Pin minor tags everywhere; `ollama/ollama` explicit version; consider digest pins + Dependabot. Switch `redis:7-alpine` → `valkey:8-alpine` (BSD, drop-in) to clear the SSPL question.
- **P4-5 Reproducible Python builds.** [P1#3] pip-tools/uv compiled lock with hashes for backend + worker; Docker `--require-hashes`.
- **P4-6 Runtime hygiene.** [P1#7, #11, 3d N15, N20] Self-host fonts (`@fontsource/*`); `.env` + `**/.env` in `.dockerignore`; celerybeat-schedule volume; Python version standardization decision (3.12 everywhere vs deliberate bump — note passlib/py3.13 landmine is cleared by P1-10).
- **P4-7 CI coverage of real failure modes.** [3d N22, 2a watchlist] Alembic `upgrade head` smoke test against the compose Postgres; docker build check for all 3 images; contract drift check (the P0-18 assertion test + a generated-OpenAPI vs api-contract.md diff check).

**Phase 4 gate:** CI green with zero ignored CVEs; `docker compose build` reproducible; fresh-clone `bootstrap.sh` → `compose up` smoke passes.

---

## PHASE 5 — Product completion & evolution (est. ongoing, 2-3 days initial)

- **P5-1 Resolve dead handoffs deliberately.** [2a#6, 2b#8, 3c#22] Decide per flow: (a) wire the approved→stealth dispatch for fiverr/PPH/guru (with P2-2 truthfulness in place first — order matters), plus a weekly competitor-scrape beat; or (b) remove the dead handlers/endpoints and fix the 501 text + add "mark as manually submitted" transition. Recommendation: (a) for fiverr buyer-request offers (highest win-rate lever), (b) for the rest until demand proves out.
- **P5-2 Platform single-source-of-truth.** [2a#19] One canonical platform registry (backend constant + worker import or an asserted-equal test covering `schemas.py:7`, `credentials.py`, `proposal_status_sync.py:33`, `discovery.py:27`, `worker/config.py:12`, `types.ts:11-18`).
- **P5-3 Celery robustness.** [2a#17, 3b N22, N27] `autoretry_for` + backoff on idempotent ticks; HALF_OPEN single-trial token (circuit_breaker.py:33-43); deployment note: single beat instance (or Redis leader lock).
- **P5-4 Correctness tail.** [3b N10, N13, N15, N18, N19, N20, N21, N25, N6] Bid caps in hourly/fiverr branches (proposal_gen.py:348-354); short-tag matching fix (fiverr_monitor.py:140-144); own-message str coercion (outcome_sync.py:44-75); digest is_active filter + TLS opt-in + daily-digest catch-up; threshold comment/code reconciliation (ingest.py:135-137); scoring nits (scoring.py:103,176-189,243); learning-loop atomic increments (templates.py:87-92, rate_learning.py:24-32); boolquery tokenizer strictness (:24-31); partial unique index for duplicate-proposal prevention (orchestrator.py:165-174).
- **P5-5 Scorecard re-run.** Re-score all 5 pillars after Phases 0-4; log to `evolution/`.

---

## Dependency order (why this sequence)

Phase 0 first because every later phase's testing depends on a pipeline that survives its own success path, and because double-submit/data-loss bugs damage real tenant reputations daily. Phase 1 before Phase 2 because stealth-session binding (P1-2) is the channel per-tenant proxies (P2-1) travel through, and wiring dead submit paths (P5-1) before submission truthfulness (P2-2) would manufacture false "submitted" audit records. Phase 4 last of the engineering phases because a fastapi major bump invalidates earlier test baselines if done first.

**Total estimate:** ~10-12 working days of focused implementation across 5 phases, 69 work items, each traced to a verified finding.

## Standing risks not fixable by code (record for honesty)
- Live-platform selector drift (worker/platforms.py) — needs first-real-session validation; designated maintenance point.
- GitHub Actions billing lock on the account — CI must be unlocked by the user.
- Ollama model pulls are unvetted binaries by nature — document, keep opt-in.
