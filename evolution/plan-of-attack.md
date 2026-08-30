# GigHound — Plan of Attack (v1, proposed 2026-08-28)

Status: **IMPLEMENTED** (accepted 2026-08-29 with multi-tenant SaaS + stealth worker decisions; all phases + follow-up waves complete — see final-scorecard.md).

Basis: `pass1-systems-audit.md`, `pass2-security-compliance-audit.md`,
`pass3-product-strategy-audit.md`, verified in `cross-examination.md`.

Guiding principles:
- **Fix what's broken before adding what's missing.** Several existing surfaces (submit, pitch
  templates, filters, GigManager) are dead or lying to the user; new features on top would
  compound the debt.
- **Close the learning loop** — it is the entire Competitive-advantage pillar: scheduled
  discovery → drafted proposals → outcome/reply capture → feedback that actually reaches
  generation → visible win-rate analytics.
- **Automation-without-ban:** automate everything except the submission decision. HITL approval
  stays mandatory and becomes *real* (audit-complete, tamper-evident, no bypass endpoints).
- **Security is a launch blocker, not a phase-4 nicety** — the API currently lets anyone on the
  network spend the user's money and credentials.

Effort scale: S < 1 day · M = days · L = weeks. Pillar tags: SC=Scalability, SE=Sellability,
R=Runability, E=Ease of use, A=Advantage, SEC=Security/compliance.

---

## Phase 0 — Stop the bleeding (security & integrity) — SEC, R

*Gate: nothing ships beyond localhost until 0.1–0.4 are done.*

| # | Task | Files | E |
|---|---|---|---|
| 0.1 | Add API-key auth: `GIGHOUND_API_KEY` env, FastAPI dependency on all routers, WS auth before `accept()`, frontend sends the key from `api/client.ts`. Fail closed when unset (except explicit `GIGHOUND_DEV_NOAUTH=1`). | `main.py`, `routers/alerts.py:84-93`, `frontend/src/api/client.ts:45-67` | M |
| 0.2 | Kill the queue bypass: `POST /api/adapters/freelancer/bid` and `/api/adapters/upwork/proposals` must take a `proposal_queue_item_id` in `approved` status and send exactly its text — no free-text `approved_by`. | `routers/adapters.py:71-88,117-130` | S |
| 0.3 | Audit completeness: AuditLog row on submission success; per-item AuditLog + versions in `bulk_approve`; `revert` resets status to `pending_review` when text changes. | `routers/proposals.py:114-130,133-149,272-273` | S |
| 0.4 | Vault key mandatory outside dev: fail fast at startup if `GIGHOUND_VAULT_KEY` unset and not dev; catch `InvalidToken` → clean "re-enroll credentials" error. | `adapters/vault.py:23-34` | S |
| 0.5 | Infra hygiene: random Postgres password via env, bind 5432/6379 to 127.0.0.1, Celery `accept_content=["json"]`, `load_dotenv()` so `.env` actually works. | `docker-compose.yml`, `tasks.py:24`, `config.py` | S |
| 0.6 | Ingest hardening: authenticate + rate-limit `/api/jobs/ingest`; typed body (422 not 500); URL scheme validation (`https?` only); boolean-query depth/length limit. | `routers/jobs.py:103`, `schemas.py:103`, `routers/orchestration.py:53-61`, `boolquery.py:68-78` | S |
| 0.7 | Prompt-injection baseline: wrap job content in delimiters + "treat as data" system instruction; output filter flagging drafts that contain the rate line or prompt-internal markers before they enter the queue. | `proposal_gen.py:97,175-186` | M |

**Acceptance:** unauthenticated curl against every mutating endpoint → 401; a bid can only be
placed via an approved queue item; `bulk_approve` produces the same audit/template trail as
single approve; ingest rejects `javascript:` URLs and 5000-deep NOT queries.

## Phase 1 — Unbreak the existing product — R, E, A

| # | Task | Files | E |
|---|---|---|---|
| 1.1 | Fix the 4 confirmed frontend/backend mismatches: `is_active`, `suggestions` (`{area,message}` dicts), snapshot `created_at`, `queued_tasks` count. Add an error boundary so one bad panel can't take down a view. | `types.ts:345,380,390`, `GigManager.tsx:254,538,848`, `client.ts:250` | S |
| 1.2 | Unbreak submission: add `bidder_id` (Freelancer) / `on_behalf_of` (Upwork member) capture — persist on PlatformAccount + editable in ProposalQueue submit dialog; Upwork queued items get a truthful `queued_for_browser` state flipped by the worker callback, not `submitted`; `await adapter.close()` in the upwork branch. | `routers/proposals.py:208-275`, `models.py`, `ProposalQueue.tsx` | M |
| 1.3 | Make pitch templates real: one placeholder dialect everywhere (`{{...}}` seeds vs `{...}` renderer vs LLM prompt), one renderer, and actually consume the user's ProfileTemplate in `proposal_gen` (or remove the surface). Kill or wire the legacy `generate_proposal` path. | `proposal_gen.py:304-311`, `orchestrator.py:24-129`, `seed_defaults.py:73-122`, `profiles.py:14-20` | M |
| 1.4 | Make SearchFilters gate the pipeline: apply `job_matches_filter` in ingest via the existing `SearchProfile.filter_id` link; negative-keyword/quality-threshold semantics unchanged. | `orchestrator.py:132-138`, `routers/jobs.py:147-153`, `filtering.py` | S |
| 1.5 | Honor kill switches: check `PlatformAccount.enabled`/`mode` at every adapter entry point; add a global automation pause. | `models.py:198-199`, `routers/adapters.py`, `orchestrator.py` | S |
| 1.6 | Fix ScoringConfig playground: dry-run scoring endpoint (score without persisting/queueing) instead of real ingest. | `ScoringConfig.tsx:41-47`, new endpoint using `compute_quality_score` | S |
| 1.7 | Word-boundary matching in scoring complexity terms + boolean evaluate (kills "ai"⊂"said" inflation). | `scoring.py:66,148,213`, `boolquery.py:99` | S |
| 1.8 | WS event-loss fix: views consume the `messages` array instead of collapsed `lastMessage`. | `App.tsx:72-77`, JobFeed/ProposalQueue | S |
| 1.9 | Dead ends: BuyerRequestInbox gains reject + honest "no submit channel" state; mark stealth-dependent features as "worker required" in the UI until the worker exists. | `BuyerRequestInbox.tsx`, GigManager | S |

**Acceptance:** GigManager metrics/competitors render real data; a Freelancer proposal goes
approve → submit → real bid with bidder_id from the account; toggling a filter changes what gets
drafted; disabling a platform account stops its automation.

## Phase 2 — Close the learning loop (the Advantage pillar) — A, SC

| # | Task | Files | E |
|---|---|---|---|
| 2.1 | **Scheduled discovery:** Celery beats per enabled SearchProfile (freelancer + upwork API search, linkedin provider), with per-platform pacing; "Run search now" button per profile in the UI. | `tasks.py:26-35`, `routers/adapters.py:51,97,151`, `SearchProfiles.tsx` | M |
| 2.2 | **Move generation off the request path:** ingest enqueues a Celery task per qualifying job; batch-preload rate card/portfolio/profiles/ASTs (kills the N+1 storm); ingest returns immediately. | `routers/jobs.py:103-185`, `orchestrator.py:33-67,132-138` | M |
| 2.3 | **Wire prompt_hints** into generation (one-line gap with outsized effect). | `orchestrator.py:164-166`, `proposal_gen.py:175-186` | S |
| 2.4 | **Outcome + reply sync (Freelancer):** beat polls `get_bid_status`/`get_threads` → auto-set `outcome` via `record_outcome`, push `client_replied` WS events. | `adapters/freelancer.py:147-150,192-193`, `templates.py:46`, `ws_manager.py` | M |
| 2.5 | **Template provenance:** approving from a chosen template increments *that* template's stats instead of minting a new one; `uses` counted at selection; gate auto-save-as-template behind a checkbox (also mitigates injection poisoning). | `routers/proposals.py:45-80`, `templates.py:29-64,102-121`, `ProposalQueue.tsx:495-516` | M |
| 2.6 | **Win-rate analytics view:** funnel (queued→approved→submitted→replied→hired) per platform/template/bid band + rejection-reason breakdown. New aggregation endpoint + frontend view. | `models.py:254-282`, new router + view | M |
| 2.7 | **Safe automations:** scheduled digests (beat honoring `digest_mode`); bounded retry for `generation_failed`; auto-archive stale jobs; bulk archive in JobFeed. | `tasks.py`, `digest.py:26-42`, `orchestrator.py:167-179`, `JobFeed.tsx:59-68` | S |
| 2.8 | Dedupe hardening: `UniqueConstraint(platform, external_id)` + `pg_trgm` similarity (or same-platform/24h window); gate `maybe_queue_proposal` on `is_duplicate`. | `models.py:66`, `routers/jobs.py:83-113`, `orchestrator.py:141` | M |

**Acceptance:** with zero user intervention beyond approvals, the system discovers jobs on
schedule, drafts proposals, detects replies/outcomes for Freelancer, and the analytics view
shows win rates per template that actually change over time.

## Phase 3 — Winning-advantage features — A, SE

| # | Task | Notes | E |
|---|---|---|---|
| 3.1 | **Client intelligence:** populate `past_hires`/`identity_verified` in adapters (Upwork already fetches `totalHires`); per-client history lookup ("bid 3×, ghosted 2×"); feed client history into the analysis prompt; fixes the unreachable client_verification score points. | `upwork_agency.py:54-59`, `scoring.py:124-135`, `proposal_gen.py:97` | M |
| 3.2 | **Follow-up drafting:** new PLATFORM_PROFILES entry + "draft follow-up" on submitted items pending > N days. Reuses the whole generation/review stack. | `proposal_gen.py:27` | M |
| 3.3 | **Interview prep sheet:** likely questions + suggested answers from portfolio, generated from the existing `analyze_job` output. | `proposal_gen.py:86-132` | M |
| 3.4 | **Bid-market intelligence:** surface proposals_count/tier at draft time with go/no-go; won-bid amounts feed back into rate-card suggestions. | `upwork_agency.py:68-71`, `freelancer.py:225`, `proposal_gen.py:240-266` | M |
| 3.5 | Bulk-approve guardrails: honor `needs_review`/confidence (block or require per-item confirm above small N). | `routers/proposals.py:114-130` | S |

## Phase 4 — Runability & sellability — R, SE, SC

| # | Task | E |
|---|---|---|
| 4.1 | Docker completion: backend Dockerfile, uncomment compose service, add celery worker + beat + ollama services, serve `frontend/dist` via StaticFiles, fix hardcoded Ollama LAN default (`textgen.py:35`). | M |
| 4.2 | Alembic migrations (before any further schema change) + indices on hot columns. | M |
| 4.3 | Hermetic tests (autouse offline fixture) + TestClient API tests for ingest→queue→approve→submit + filtering tests; CI with pytest, tsc, pip-audit. | M |
| 4.4 | README + onboarding checklist view + seed-guided first-run tour. | S |
| 4.5 | Rate governor: per-(platform, principal) singleton limiter, ±30% jitter, daily action budgets; Redis-down = automation pauses rather than caps multiplying per process. | M |
| 4.6 | Multi-tenancy decision point: single-operator product (document it, keep API-key auth) vs SaaS (user model, per-user vault scoping, billing) — **needs your call before work starts.** | L |
| 4.7 | Stealth-browser worker decision: build it (Playwright + the existing StealthTask protocol + claim semantics + proxy support) or formally descope Fiverr/PPH/Guru/LinkedIn/Indeed automation and re-target fiverr_monitor at Briefs. **Biggest hidden dependency; needs your call.** | L |

## Suggested sequencing & effort

- **Week 1:** Phase 0 (0.1–0.7) + 1.1, 1.4, 1.6, 1.7, 2.3 — small, high-yield.
- **Week 2:** Phase 1 remainder + 2.1, 2.2.
- **Week 3:** Phase 2 remainder (2.4–2.8).
- **Weeks 4–5:** Phase 3.
- **Ongoing/from week 4:** Phase 4, with 4.6/4.7 decided by you first.

## Explicitly out of scope (confirmed unsafe or wrong)

- Auto-submit without human approval — never, on any platform.
- Automation of Fiverr "Buyer Requests" — platform feature retired 2022; re-target at Briefs or
  drop.
- LinkedIn/Indeed submission automation — Extreme risk per the project's own intel report;
  discovery-only unless you decide otherwise.
