# Pass 1 — Systems / Code Archaeology Report

**Date:** 2026-08-28 · **Perspective:** full-stack code & data-flow audit, every file read end to end.
**Verified empirically:** `pytest` → 74 passed; `tsc -b` → clean; app + Celery import smoke-tested; `seed_defaults` run twice against SQLite (idempotent).

## 1. Architecture map — the real data flow

### Ingest → queue (all synchronous inside one HTTP request)

```
POST /api/adapters/{freelancer,upwork,linkedin}/search  (routers/adapters.py:51,97,151)
  → adapter.search_jobs() → JobPosting[] → to_ingest()
  → routers/jobs.ingest_jobs()  (also exposed directly as POST /api/jobs/ingest, jobs.py:103)
      per job:
        dedupe by (platform, external_id)            jobs.py:113
        score: compute_quality_score (scoring.py:219) with market rate from rate card (jobs.py:72)
        fuzzy cross-platform duplicate check          jobs.py:83 (_find_duplicate)
        auto-archive below min(filter thresholds)     jobs.py:148
        WS broadcast {type: job_ingested}             jobs.py:161
        orchestrator.maybe_queue_proposal()           orchestrator.py:141
          gate: not archived, no existing queue item, boolean-profile match
          templates.generation_tuning() + top_templates() (few-shot)
          proposal_gen.generate()                     proposal_gen.py:271
            analyze_job (LLM JSON or heuristic) → skill_portfolio_match
            → LLM draft or offline composer → antidetect.humanize
            → calculate_bid → confidence
          insert ProposalQueueItem(pending_review) + AuditLog
          WS broadcast {type: proposal_queued}        orchestrator.py:208
          on exception: ProposalQueueItem(generation_failed) + WS generation_failed (orchestrator.py:167)
        hot-job / job_alert WS broadcast, status→notified  jobs.py:170
```

### Review → submit

```
GET /api/proposals?status=pending_review  (proposals.py:29, embeds Job per item — N+1)
POST /api/proposals/{id}/approve  → status=approved, save_as_template() (templates.py:29), AuditLog
POST /api/proposals/{id}/reject   → RejectionFeedback → generation_tuning temperature dampening
POST /api/proposals/{id}/submit   (proposals.py:208)
    freelancer → FreelancerAdapter.place_bid (official API, monthly quota in AdapterState)
    upwork     → UpworkAgencyAdapter.submit_proposal → AdapterState "pending_submissions"
                 (browser handoff; requires approved_by + agency roster membership)
    other      → 501
POST /api/proposals/{id}/outcome  → Template win_rate update (templates.py:46)
```

### Seller/gig mode (Celery + stealth-task handoff)

```
celery beat (tasks.py:26): fiverr_buyer_request_tick every 15 min, gig_analytics_tick Mon 06:12 UTC
  → StealthTask rows (pending) → external browser worker polls GET /api/gigs/stealth-tasks
  → worker POSTs results: /gigs/buyer-requests/process → process_buyer_requests
      → Job(platform=fiverr, job_type=gig) + ProposalQueueItem(request_type=buyer_request)
        (10 offers/day Redis counter, circuit-breaker gated)  fiverr_monitor.py:150
  → /gigs/metrics, /gigs/competitors, /gigs/stealth-tasks/{id}/complete
    (≥3 lifetime failures → circuit_breaker.open_circuit)
```

### WS events actually emitted
`job_ingested`, `job_alert`, `hot_job` (jobs.py:161,179), `proposal_queued`, `generation_failed`
(orchestrator.py:175,208). The documented `digest` WS type (contract line 110) is **never
broadcast** — `useAlertsSocket.ts:11` and AlertsPanel handle a message that cannot arrive.

## 2. Contract drift vs `docs/api-contract.md`

**Documented but missing/divergent:**
- `WS {type:'digest', jobs}` — never sent (contract:110).
- Contract v1 scoring weights (line 125) stale; v2 addendum matches code. v1 block never corrected.
- v2 `ProposalStatus` (contract:156) lacks `generation_failed`; corrected only in v3.

**Implemented but undocumented:**
- Entire `/api/adapters/*` surface (freelancer search/bid/quota, upwork search/proposals/agency
  members, linkedin search — routers/adapters.py). No frontend calls them either.
- `POST /api/profiles/templates/generate` (profiles.py:23) and
  `POST /api/proposals/templates/generate` (proposals.py:159).
- `GET /api/health` (main.py:39).

**Type/field mismatches (schemas.py ↔ frontend/src/types.ts) — all confirmed:**
- `GigTemplate.is_active` (schemas.py:292) vs frontend `GigTemplate.active` (types.ts:380).
  Toggle in `GigManager.tsx:538` reads `t.active` → always `undefined` → every template shows
  "Activate", even active ones. **Verified.**
- `GigMetric.suggestions`: backend `list[dict]` `{area, message}` (gig_analytics.py:28,
  schemas.py:325) vs frontend `string[]` (types.ts:345). `GigManager.tsx:254-256` does
  `<li key={s}>{s}</li>` with an object → **React render crash** whenever a gig has suggestions.
  No error boundary — takes down the whole view. **Verified.**
- `CompetitorSnapshot`: backend emits `{id, platform, category, gigs, insights, created_at}`
  (schemas.py:332); frontend type is `{date, gigs, insights}` (types.ts:390).
  `GigManager.tsx:848-849` uses `key={s.date}` (all undefined — duplicate-key hazard) and
  `<h3>{s.date}</h3>` renders empty. **Verified.**
- `POST /api/gigs/metrics/scrape` returns `{queued_tasks: number[]}` (gigs.py:159) but frontend
  `triggerGigScrape` types `{enqueued?: number}` (client.ts:250) — count never displayed.
- `JobType` in `adapters/schema.py:23` excludes `"gig"` while `schemas.JobType` includes it —
  two parallel enums already diverged; `fiverr_monitor` creates `job_type="gig"` rows directly
  (fiverr_monitor.py:178).

## 3. Function-level issues

### main.py / config.py / database.py / cache.py
- `main.py:34` — deprecated `@app.on_event("startup")`; `create_all` is the only schema
  management: **no migrations**, column changes silently won't apply to existing DBs.
- `config.py` — **no `load_dotenv()` anywhere** (grep-verified). `python-dotenv` is in
  requirements but unused; copying `.env.example` → `.env` does nothing. Only exported env
  vars work.
- `cache.py:16-27` — `_NullCache` dead code.
- Private-attribute poking of `cache._r` from textgen.py:102, antidetect.py:103,
  fiverr_monitor.py:31, circuit_breaker.py:26. If Redis dies *after* boot, `cache._r` stays set
  and every call raises raw `redis.RedisError`.

### scoring.py
- `scoring.py:134` — comment says "past hires ≤6" but code is `hires/10` → 60+ hires for full
  points. Comment drift.
- `COMPLEXITY_TERMS` substring matching (lines 66,148,213): `"ai" in t` matches "said",
  "available"; `"api" in t` matches "capital". Complexity/tech-hit counts systematically
  inflated. Needs word-boundary regex.
- Negative keywords (line 84): naive substring — negative "php" kills "graphPHP".
- `score_urgency_ratio` (line 184): `age_h <= 1` branch adds `0.0` — dead branch.
- `detect_red_flags` param `posted_urgency_words=True` (line 191) never used — dead parameter.

### boolquery.py
- `_tokenize` (lines 21-28): only the *tail* after the last match is validated; garbage
  **between** tokens (e.g. `react & python`) is silently swallowed — `&` disappears.
- `evaluate` TERM is substring match (line 99): query term `"AI"` matches "sAId".

### orchestrator.py
- `generate_proposal` / `GENERIC_TEMPLATE` / `_render` (lines 24-129) are **legacy dead code** —
  only called from tests (grep-verified). Live path is `proposal_gen.generate`. Tests even
  assert behavior of the dead one (test_orchestration.py:157).
- `_render` uses `str.format` with `{job_title}` keys, but seed_defaults.py:73-122 seeds
  templates with **double-brace** `{{client_name}}` placeholders — `str.format` renders them
  literally as `{client_name}`.
- **Verified:** in the live path, `proposal_gen.py:310` checks `"{job_title}" in
  tpl.pitch_template` — false for all seeded `{{...}}` templates — and the body is literally
  `pass` (line 311). **User-editable Profile pitch templates have zero influence on generated
  proposals.** A whole CRUD surface (profiles.py:56-93, ProfileManager TemplatesTab) edits text
  nothing consumes.
- `maybe_queue_proposal` does not check `job.is_duplicate` — cross-posted duplicates each get a
  full LLM proposal draft.
- `select_portfolio_items` (line 35) and `pick_rate` (line 55) do `SELECT *` per job per call
  site — `pick_rate` is called 3× per job during ingest. N+1 on batch ingest.
- Dedupe gate (lines 145-151) treats `generation_failed` as blocking — one transient Ollama
  timeout permanently suppresses re-generation for that job.

### proposal_gen.py
- `proposal_gen.py:283` — `job.bid_period_days = bid_days` monkey-patches a non-column attribute
  onto the ORM instance to smuggle data into `_generate_offline`. **Verified.**
- Confidence formula (line 319): `55 + 25*coverage`; zero-skill jobs yield coverage 0.5 → base
  67.5; `needs_review` (<50) nearly unreachable. Collected, barely gates anything.
- `_generate_with_llm` passes `max_tokens=700` (line 187) → the qwen3 reasoning-retry branch
  requires `max_tokens is None` (textgen.py:213) → **the reasoning-retry can never fire for
  proposal drafts**; reasoning-only responses silently downgrade every draft to the offline
  composer.
- `PLATFORM_PROFILES` fallback is `"guru"` (line 166) for unknown platforms — silent mis-toning.

### textgen.py / llm.py
- `resolve_provider()` (textgen.py:65) **can never return `"none"`** despite the docstring —
  fallback is always `"ollama"`. So `llm_available()` is always True and every proposal attempts
  HTTP first; offline machines pay 2 serialized connect failures per draft.
- Default non-Docker `OLLAMA_BASE_URL` is a **hardcoded private LAN IP**
  `http://192.168.1.68:11434/v1` (textgen.py:35) — someone's home network in the repo.
- Tests not hermetic: `test_orchestration.py` lacks the `force_offline` fixture its sibling has
  (test_generation_gigs.py:25) — it made real LLM calls during the audit run.

### antidetect.py
- `strip_ai_tells` (line 144): the "keep at most one AI-tell word" logic lets two different tell
  words each survive once ("Furthermore … Moreover …"). Minor gap vs stated intent.
- Openings are picked (antidetect.py:209 `opening_suggestion`) but **never spliced into the
  humanized text** — `humanize` returns a suggestion no caller uses. Dead output.

### templates.py
- `save_as_template` is called on **every single approve** (proposals.py:72) — one new Template
  row per approval; `uses` increments only at outcome time (templates.py:57), so `uses` really
  means "outcomes recorded". `top_templates` filter `Template.uses >= 0` (line 107) is a
  tautology — dead condition.
- **Verified:** `bulk_approve` (proposals.py:114-130) does **not** save templates and writes
  **no AuditLog** — the learning loop and audit trail have a hole exactly where volume is highest.

### digest.py / ws_manager.py
- `send_digest_email`: `starttls()` unconditionally (digest.py:38), no error handling.
- `ws_manager.broadcast` is process-local: Celery workers / multiple uvicorn workers share
  nothing. Fine today, breaks under horizontal scaling.
- No WS authentication or origin check (alerts.py:84).

### fiverr_monitor.py
- **Conceptual staleness:** the entire buyer-request monitor implements Fiverr "Buyer Requests",
  removed Oct 2022 and replaced by Fiverr Briefs (per docs/platform-intelligence-report.md:13,25).
  Well-built infrastructure for a retired platform feature.
- `_counter`/`_peek` local fallback (lines 26-48) is per-process: API and Celery keep **separate**
  offer counters without Redis — the 10/day cap holds only with Redis up.
- `process_buyer_requests` creates `Job` rows with fixed `quality_score=60.0` (line 182) —
  bypasses the entire scoring/filtering system yet appears in the Job Feed.

### gig_analytics / gig_templates / circuit_breaker
- `enqueue_metrics_scrape` (gig_analytics.py:83): N+1 (distinct platforms, then per-platform).
- `circuit_breaker.is_closed` (line 43): `HALF_OPEN` returns True unconditionally — nothing
  tracks the "one trial task" the docstring promises; half-open == closed.
- Circuit breaker `_local` fallback diverges across processes (same caveat as fiverr counters).

### adapters/*
- `ratelimit.py:54-68`: semaphore leak if the coroutine is cancelled while waiting on the lock.
- `ratelimit.py:70-75`: control flow after final attempt relies on retryable statuses always
  raising in `raise_for_status()`.
- `vault.py:23-34`: ephemeral key is **per-process** — without `GIGHOUND_VAULT_KEY`, credentials
  written by the API process are undecryptable by the Celery worker (silent `InvalidToken`).
- `upwork_agency.get_job_details` (line 144) searches by expression, not ID — can return the
  wrong job; raises `AdapterAuthError` for "not found" (wrong class, line 150).
- `linkedin._search_brightdata` polls up to 2 min of `asyncio.sleep` inside an HTTP request
  handler — hung-worker territory.
- `StateStore.set` (vault.py:84): read-modify-write with no uniqueness constraint on
  `(platform, key)` — concurrent bid placements can both pass the quota check (race in
  `FreelancerAdapter.place_bid`, freelancer.py:187-189).

### routers
- `jobs.ingest_jobs(body: dict)` (jobs.py:104): untyped body → pydantic `ValidationError`
  surfaces as **500, not 422**. Whole batch pipeline (scoring + dedupe + LLM per job) runs
  **inside the HTTP request** — a 50-job batch with slow Ollama blocks for minutes.
- `jobs._find_duplicate` (jobs.py:83): loads the 200 most recent jobs *per ingested job*,
  O(n×200) fuzzy comparisons in Python; misses duplicates older than 200 rows; skips archived
  candidates.
- `proposals.list_proposals` (proposals.py:29-34): N+1 — `_with_job` does `db.get(Job, ...)` per
  item (up to 200 queries per list call). **Verified.**
- `proposals.submit_proposal` (proposals.py:208) — **all verified by direct read:**
  - `bidder_id` (freelancer) and `on_behalf_of` (upwork) are read from `item.submission_result`
    (lines 229, 253) — **nothing anywhere in the system ever writes those keys**, and the
    frontend has no field for them. Freelancer submissions 400; upwork submissions fail with
    "not an agency member" → item flips to `failed`. **The submit button is effectively broken
    for both supported platforms.**
  - Upwork path sets `status="submitted"` (line 272) when the proposal was merely **queued** for
    browser execution — materially false state.
  - `adapter.close()` missing for the upwork branch (leaks the httpx client).
- `gigs.complete_stealth_task` (gigs.py:215): failure count is **lifetime**, not recent — 3
  failures ever trips the circuit. No `claimed` transition: the status exists in the model
  comment (models.py:358) but no endpoint sets it — multiple browser workers will double-execute
  tasks.
- `gigs.register_gig` (gigs.py:128): `body["platform"]` → KeyError → 500 on missing field.
- `orchestrator._matches_any_profile` (line 138) re-parses each profile's boolean query for
  every job — cache the AST.

### models.py
- No `UniqueConstraint(platform, external_id)` on `Job` (models.py:66) — concurrent ingests can
  double-insert; dedupe is application-level only.
- No `UniqueConstraint(platform, principal)` on `AdapterCredential`, nor `(platform, key)` on
  `AdapterState` — vault read-modify-write can create duplicate rows under races.
- Missing indices: `jobs.fetched_at` (every list ordering), `proposal_queue.created_at`,
  `gig_metrics.week`, `stealth_tasks.created_at`.
- Naive-datetime defensive patches in `filtering._aware` and `scoring.py:165,182` are evidence
  naive/aware bugs have bitten before.

## 4. Runability audit

**Backend + tests + type-check pass from a fresh clone; the *system* is not runnable as designed.**

- ✅ `pip install -r requirements.txt && pytest` → 74/74 pass (2937 warnings, dominated by
  pytest-asyncio 0.23 loop-policy deprecations).
- ✅ `npm install && tsc -b` → clean (the §2 mismatches are type-level lies: the interfaces
  *declare* the wrong field names, so the compiler is happy).
- ✅ `seed_defaults` idempotent on SQLite and Postgres.
- ❌ **`.env` is never loaded** — no `load_dotenv` anywhere.
- ❌ **No Docker story**: compose has the backend commented out and there is **no
  `backend/Dockerfile`** — `build: ./backend` would fail. The comment references an `ollama`
  service that doesn't exist in the file. No Celery worker/beat services.
- ❌ **No frontend production serving**: `vite preview` only; FastAPI doesn't serve
  `frontend/dist`.
- ⚠️ Nothing schedules digest emails despite `digest_mode` hourly/daily settings — only the
  manual `/digest/send` endpoint; the `digest` WS type is dead.
- ⚠️ `requirements.txt` ships pytest in main deps; `websockets` and `python-dotenv` unused.
- ⚠️ Hardcoded private LAN Ollama default (textgen.py:35).
- ⚠️ Tests not hermetic (real LLM calls possible, above).

## 5. Test coverage

**74 tests across 4 files.** Well-covered: scoring v2, boolean parser, adapter
normalization/OAuth/quota/audit (fully mocked), textgen provider resolution + error taxonomy,
anti-detect primitives, offline proposal pipeline, template learning, gig validation, fiverr
monitor, circuit gating.

**Critical untested paths:**
- **Zero API-level tests** — no `TestClient` anywhere. The entire FastAPI surface (ingest,
  approve/reject/submit, gigs, alerts) is untested end-to-end; every router bug above would be
  caught by even shallow API tests.
- `filtering.job_matches_filter` — completely untested.
- `orchestrator._matches_any_profile`, duplicate-flag gating, `generation_failed` blocking.
- `routers/jobs._find_duplicate` (quadratic dedupe).
- `ratelimit.request_with_retry` (backoff, Retry-After, exhaustion).
- `circuit_breaker` half-open/cooldown; `gigs.complete_stealth_task` tripping.
- `digest`, `cache` degrade path, `ws_manager.broadcast` reaping.
- Frontend: no tests at all (no runner configured).

## 6. Frontend audit

- **Event-loss bug** (App.tsx:72-77 + React 18 batching): ingest fires `job_ingested` →
  `proposal_queued` → `job_alert` in rapid succession; multiple `setLastMessage` calls within one
  batch collapse, so JobFeed/ProposalQueue can miss messages. AlertsPanel is immune (uses the
  `messages` array at useAlertsSocket.ts:88); other views should consume that array too.
- **WS hook** (useAlertsSocket.ts): correct exponential backoff (1s→30s), ping every 30s,
  StrictMode-safe, `messages` capped at 50. Solid.
- **api/client.ts**: single `request<T>` wrapper, `ApiError` with parsed detail, 204 handling.
  No timeout/abort, no retry; error message embeds raw body.
- **Runtime-breaking bugs** (all in GigManager): suggestions-as-objects crash, `s.date`
  undefined, `active` vs `is_active`, `{enqueued}` vs `{queued_tasks}` — two tabs partially or
  wholly broken against the real API.
- **Dead/broken flows**: ProposalQueue "Submit to platform" can never succeed (no
  `bidder_id`/`on_behalf_of` source). ScoringConfig "Test the scorer" **creates real DB rows and
  can trigger real LLM proposal generation** — test jobs (`external_id=test-*`) pollute the feed,
  rate counters, and template library.
- **BuyerRequestInbox** fetches all proposals (≤200) and filters client-side; shows
  approved/rejected alongside pending; no live updates.
- **JobFeed** (JobFeed.tsx:50): WS-inserted jobs bypass the active status/platform filters.
- **Accessibility**: Modal has no focus trap / Escape handler; drawers are onClick divs;
  icon-only buttons rely on `title`; no `aria-live` on toasts.
- **Duplicated logic**: reviewer-name/localStorage in ProposalQueue + BuyerRequestInbox;
  `editsFrom`; `numOrNull`; CRUD modal scaffolding ×6 views — a generic `useCrudResource` hook
  would delete hundreds of lines.
- **No dead views** — all 11 nav targets are wired and functional modulo the bugs above.

## 7. Optimization opportunities (concrete)

1. **Batch ingest pipeline** (jobs.py:103-185): hoist rate-card/portfolio/profile loads and
   parsed boolean ASTs out of the per-job loop (~8 redundant queries + 200-row fuzzy scan *per
   job* → ~6 queries total). Move `maybe_queue_proposal` (LLM calls!) into a Celery task.
2. **Fix `_find_duplicate`** (jobs.py:83): `pg_trgm` + `similarity()` on Postgres, or at minimum
   same-platform + recent-24h candidates.
3. **Kill the legacy generator** (orchestrator.py:24-129) or wire it; unify the placeholder
   dialect (seed `{{...}}` vs `_render` `{...}` vs profiles.py:17 instructing the LLM to emit
   `{{job_title}}` — tokens no renderer consumes). One template system, one syntax, one renderer.
4. **Unbreak the submit flow**: `bidder_id`/`on_behalf_of` inputs (or persist on PlatformAccount);
   stop marking upwork items `submitted` at queue time (`queued_for_browser` state flipped by
   worker callback); `await adapter.close()` in the upwork branch.
5. **Word-boundary matching** in scoring.COMPLEXITY_TERMS and boolquery.evaluate.
6. **Real provider detection** (textgen.py:65): allow `LLM_PROVIDER=none`; let reasoning-retry
   apply when the caller passes `max_tokens`.
7. **Schema integrity**: UniqueConstraints on Job(platform, external_id),
   AdapterCredential(platform, principal), AdapterState(platform, key); indices on hot columns;
   Alembic before the next column change.
8. **N+1 eliminations**: proposals.list → joinedload; gig_analytics.enqueue_metrics_scrape →
   grouped query.
9. **Fix the four frontend/backend field mismatches** — one line each; currently crash or blank
   two tabs.
10. **Claim protocol for stealth tasks** (gigs.py:204): atomic
    `UPDATE ... WHERE status='pending'`; windowed circuit failure counting.
11. **Compose the missing services**: backend Dockerfile, celery worker + beat, ollama service
    (or remove the reference), static frontend serving; `load_dotenv()` in config.py.
12. **Hermetic tests**: autouse offline fixture; TestClient API tests for
    ingest→queue→approve→submit.
13. **Half-open trial accounting** in circuit_breaker.py:43.
14. **Reassess fiverr_monitor's premise** (Buyer Requests retired 2022): re-target at Briefs or
    demote to a stub.
