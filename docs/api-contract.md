# GigHound — API Contract (frontend ↔ backend)

Base URL: `http://localhost:8000`. All JSON. CORS enabled for `http://localhost:5173`.

## Domain types (mirror these in TypeScript)

```ts
type Platform = 'upwork'|'fiverr'|'freelancer'|'peopleperhour'|'guru'|'linkedin'|'indeed';
type KeywordKind = 'primary'|'secondary'|'negative';
type JobType = 'fixed'|'hourly'|'retainer'|'contest';
type ExperienceLevel = 'entry'|'intermediate'|'expert';
type WorkArrangement = 'remote'|'onsite'|'hybrid';
type JobStatus = 'new'|'notified'|'archived';

interface Keyword { id?: number; term: string; kind: KeywordKind; weight: number; } // weight 0.0–1.0 (primary); ignored for negative
interface KeywordGroup { id: number; name: string; service_type: string; keywords: Keyword[]; created_at: string; }

interface ClientFilters {
  payment_verified?: boolean|null;
  min_hire_rate?: number|null;      // 0–100
  min_total_spent?: number|null;    // USD
  countries?: string[];             // ISO codes
}
interface PlatformBudget { platform: Platform; min?: number|null; max?: number|null; currency: string; }

interface SearchFilter {
  id: number; name: string;
  keyword_group_id?: number|null;
  platforms: Platform[];
  job_types: JobType[];
  budgets: PlatformBudget[];                 // per-platform ranges, normalized to USD server-side
  experience_levels: ExperienceLevel[];
  client_filters: ClientFilters;
  posted_within_hours?: number|null;
  apply_deadline_within_hours?: number|null;
  work_arrangements: WorkArrangement[];
  languages: string[];
  max_proposals?: number|null;               // skip oversaturated jobs
  quality_threshold: number;                 // 0–100, auto-archive below this
  created_at: string;
}

interface ScoreBreakdown { [component: string]: number; } // e.g. keyword_match, budget_realism, client_quality, description_quality, urgency, red_flag_penalty
interface Job {
  id: number; external_id: string; platform: Platform;
  title: string; description: string; url: string;
  job_type: JobType|null;
  budget_min: number|null; budget_max: number|null; currency: string;
  budget_usd_min: number|null; budget_usd_max: number|null;
  experience_level: ExperienceLevel|null;
  client_info: { payment_verified?: boolean; hire_rate?: number; total_spent?: number; country?: string; rating?: number; reviews_count?: number };
  proposals_count: number|null;
  skills: string[]; languages: string[];
  work_arrangement: WorkArrangement|null;
  posted_at: string|null; apply_deadline: string|null;
  quality_score: number; score_breakdown: ScoreBreakdown;
  red_flags: string[];
  status: JobStatus;
  is_duplicate: boolean; duplicate_of: number|null;
  fetched_at: string;
}
interface JobIngest { /* Job minus id/quality_score/score_breakdown/red_flags/status/is_duplicate/duplicate_of/fetched_at */ }

interface AlertSettings {
  realtime_enabled: boolean;
  min_score_alert: number;          // only alert jobs ≥ this score
  digest_mode: 'off'|'hourly'|'daily';
  hot_job_enabled: boolean;
  hot_job_max_proposals: number;    // "hot" = score ≥ min_score_alert AND proposals ≤ this AND posted within hot_job_posted_hours
  hot_job_posted_hours: number;
}

interface ProfileTemplate { id: number; platform: Platform; name: string; pitch_template: string; created_at: string; }
interface PortfolioItem { id: number; title: string; description: string; url: string; tags: string[]; created_at: string; }
interface RateCardEntry { id: number; skill_category: string; hourly_rate: number|null; fixed_min: number|null; currency: string; }
```

## REST endpoints

All endpoints below except `/api/auth/register` and `/api/auth/login` require
`Authorization: Bearer <jwt>` (12h expiry). Error envelope is FastAPI's
`{detail: ...}`; the SPA drops the session on any **401** outside `/api/auth/*`.

### Auth
- `POST /api/auth/register` body `{email, password, display_name}` → **201** `{access_token, token_type, user}` — **409** email taken, **403** registration disabled, **429** rate-limited (5/hour/IP)
- `POST /api/auth/login` body `{email, password}` → `{access_token, token_type, user}` — **401** bad credentials (unknown emails get a dummy verify, same timing), **403** account disabled, **429** rate-limited (5 failures/5min per email+IP, 20 per IP)
- `GET /api/auth/me` → `User` (`{id, email, display_name, is_active, created_at}`)
- `POST /api/auth/logout` → `{status: "ok"}` — revokes the token's jti until its exp (fail-open when Redis is down)
- `POST /api/auth/password` / `DELETE /api/auth/account` — see Addendum v8

### Keyword intelligence
- `GET /api/keyword-groups` → `KeywordGroup[]`
- `POST /api/keyword-groups` body `{name, service_type, keywords: Keyword[]}` → `KeywordGroup`
- `PUT /api/keyword-groups/{id}` same body → `KeywordGroup`
- `DELETE /api/keyword-groups/{id}` → 204
- `GET /api/skills/suggest?platform=upwork&q=rea` → `{suggestions: string[]}` (platform-specific skill taxonomy)

### Search fine-tuning
- `GET /api/filters` → `SearchFilter[]`
- `POST /api/filters` body = SearchFilter minus `id,created_at` → `SearchFilter`
- `PUT /api/filters/{id}` → `SearchFilter`
- `DELETE /api/filters/{id}` → 204
- `POST /api/filters/{id}/preview` → `{matched: Job[], excluded_count: number}` (runs filter against cached/DB jobs, explains matches)

### Jobs feed & scoring
- `GET /api/jobs?status=new&platform=upwork&min_score=60&limit=50&offset=0` → `{jobs: Job[], total: number}`
- `GET /api/jobs/{id}` → `Job` (includes score_breakdown + red_flags)
- `POST /api/jobs/{id}/archive` → `Job`
- `POST /api/jobs/{id}/unarchive` → `Job`
- `POST /api/jobs/ingest` body `{jobs: JobIngest[]}` → `{ingested: number, auto_archived: number, alerts_sent: number}` (adapter entry point; scores every job, auto-archives below threshold, pushes WS alerts)
- `POST /api/jobs/score-preview` body `{job: JobIngest}` → `{quality_score: number, score_breakdown: Record<string, number>, red_flags: string[]}` — dry-run scoring for the ScoringConfig playground; persists nothing, queues nothing; negative-keyword semantics identical to ingest

### Alerts
- `GET /api/alerts/settings` → `AlertSettings` (singleton, auto-created)
- `PUT /api/alerts/settings` body = `AlertSettings` → `AlertSettings`
- `GET /api/alerts/digest-preview` → `{jobs: Job[]}` (what the next digest would contain)

### Adapters (`/api/adapters` — discovery → ingest bridge + gated write actions)
Write actions take an APPROVED review-queue item id and send exactly the queued text/bid — caller-supplied content is never accepted (**409** unless `approved`). A disabled PlatformAccount → **409** on any of these.
- `POST /api/adapters/freelancer/search` body `{query, limit, location, remote_only, sandbox, auto_ingest}` → `{found, ingest, jobs[]}` — **502** on upstream failure
- `POST /api/adapters/freelancer/bid` body `{proposal_queue_item_id}` → `{bid, bids_remaining}` — **400** when no `bidder_id`, **429** monthly quota depleted
- `GET /api/adapters/freelancer/quota` → `{monthly_quota, bids_remaining}`
- `POST /api/adapters/upwork/search` — same body/response as freelancer search
- `POST /api/adapters/upwork/proposals` body `{proposal_queue_item_id}` → `{queued}` — enqueues the stealth submission; **400** when no `on_behalf_of`, **409** circuit open
- `GET/POST /api/adapters/upwork/agency/members`, `DELETE /api/adapters/upwork/agency/members/{username}` — agency member roster
- `POST /api/adapters/linkedin/search` — same body/response as freelancer search (provider selected via `LINKEDIN_PROVIDER`)

### WebSocket
- Auth handshake: `POST /api/alerts/ws-ticket` → `{ticket}` — a one-time, 30s
  ticket (keeps the JWT out of WS query strings / access logs). **503** when
  the Redis ticket store is down; the client then falls back to the legacy
  path. Connect `WS /ws/alerts?ticket=<ticket>` — single-use, verified before
  `accept()`. Legacy fallback: `WS /ws/alerts?token=<jwt>`. Auth failure →
  close code **4401** (the SPA drops the session, same as a 401).
- Server pushes JSON messages:
  - `{type: 'job_alert', job: Job}` — high-match job
  - `{type: 'hot_job', job: Job}` — urgency alert
  - `{type: 'proposal_status_changed', proposal_id: number, status: string}` —
    browser-worker submission verdict (`submitted` / `failed` / `submitted_unverified`)
  (digests are email-only — there is no `digest` WS message.)
  Client should send `ping` text every 30s; server tolerates it.

### Profile management
- `GET /api/profiles/templates?platform=upwork` → `ProfileTemplate[]`
- `POST /api/profiles/templates` body `{platform, name, pitch_template}` → `ProfileTemplate`
- `POST /api/profiles/templates/generate` body `{platform, notes?, temperature?, max_tokens?, timeout?}` → `{text, model, provider, latency_ms, offline, warning?}` — LLM-drafted pitch template (deterministic offline fallback); never auto-saves, save via the CRUD endpoints
- `PUT /api/profiles/templates/{id}` / `DELETE /api/profiles/templates/{id}`
- `GET /api/profiles/portfolio` → `PortfolioItem[]`
- `POST /api/profiles/portfolio` body `{title, description, url, tags}` → `PortfolioItem`
- `PUT /api/profiles/portfolio/{id}` / `DELETE /api/profiles/portfolio/{id}`
- `GET /api/profiles/rate-card` → `RateCardEntry[]`
- `POST /api/profiles/rate-card` body `{skill_category, hourly_rate, fixed_min, currency}` → `RateCardEntry`
- `PUT /api/profiles/rate-card/{id}` / `DELETE /api/profiles/rate-card/{id}`

## Scoring (server-side, for UI display)
`quality_score` 0–100 = keyword_match(30) + budget_realism(25) + client_quality(15) + description_quality(20) + urgency(10) − red_flag_penalty(0–40), clamped 0–100. Negative keyword hit = score 0 and immediate exclusion. `red_flags` lists human-readable flags ("unlimited revisions", "test task before hire", "work for exposure", "no upfront payment", "unrealistic budget", "duplicate posting", "bait-and-switch").

---

# Contract Addendum v2 — Orchestration Engine (2026-08-08)

## Changed
- `JobType` now includes `"gig"` (Fiverr-style gigs / Upwork Project Catalog).
- `ClientInfo` adds `identity_verified?: boolean` and `past_hires?: number`.
- `AlertSettings` adds `hot_job_min_score` (default 90); defaults changed: `hot_job_max_proposals` 5, `hot_job_posted_hours` 1. Hot job = score ≥90 AND proposals <5 AND posted <1h.
- `SearchFilter.quality_threshold` default is now **40**.
- Score breakdown keys changed (new maxima): `keyword_match`(25), `budget_realism`(25), `client_verification`(20), `description_quality`(20), `urgency_ratio`(10), `red_flag_penalty`(0 to −60).
- New WS message types on `/ws/alerts`:
  - `{type: 'job_ingested', job: Job}` — every newly ingested job (live feed)
  - `{type: 'proposal_queued', proposal_id: number, job: Job}` — orchestrator drafted a proposal

## New types

```ts
interface SearchProfile {
  id: number; name: string;
  keyword_group_id?: number|null; filter_id?: number|null;
  boolean_query: string;             // "(React OR Next.js) AND (NOT WordPress)"
  auto_queue_proposals: boolean;
  created_at: string;
}
interface PlatformAccount {
  id: number; platform: Platform; label: string; principal: string;
  mode: 'api'|'stealth'|'hybrid'|'disabled'; enabled: boolean;
  credential_ref: string; created_at: string;
  settings: Record<string, unknown>;   // recognized keys: bidder_id (freelancer user id), on_behalf_of (upwork agency member)
}
type ProposalStatus = 'pending_review'|'approved'|'rejected'|'submitted'|'queued_for_browser'|'failed';
interface ProposalQueueItem {
  id: number; job_id: number; platform: Platform;
  proposal_text: string; bid_amount: number|null; bid_period_days: number|null;
  portfolio_item_ids: number[]; template_id: number|null;
  status: ProposalStatus; reviewed_by: string|null; reviewed_at: string|null;
  submission_result: Record<string, unknown>; created_at: string;
  job?: Job;                          // embedded on responses
}
interface ProposalReviewAction { reviewer: string; proposal_text?: string; bid_amount?: number; bid_period_days?: number; template_id?: number; save_as_template?: boolean; }
```

## New endpoints

### Proposal review queue (human-in-the-loop boundary)
- `GET /api/proposals?status=pending_review&request_type=job&limit=50&offset=0` → `{items: ProposalQueueItem[], total: number}` — always the paginated envelope (limit default 50, max 200; `total` is the full filtered count; `request_type` filters `job`/`buyer_request`/`follow_up`)
- `GET /api/proposals/{id}` → `ProposalQueueItem`
- `POST /api/proposals/{id}/approve` body `ProposalReviewAction` → item (reviewer may edit text/bid before approving; 409 unless pending_review; optional `template_id` reuses a tenant-owned template instead of minting one — 404 if unknown/foreign; `save_as_template` defaults true, set false to skip minting). Approving a fiverr `buyer_request` item also dispatches it to the browser worker (`submit_fiverr_offer` stealth task → status `queued_for_browser`, same outcome mapping as upwork); without an enrolled fiverr account or with the circuit open it stays `approved` (a `dispatch_note` in `submission_result` says why)
- `POST /api/proposals/{id}/reject` body `{reviewer}` → item
- `POST /api/proposals/{id}/submit` → item — dispatches via compliant channel: freelancer→official bid API → status `submitted` (bidder_id from `submission_result.bidder_id`, falling back to the freelancer account's `settings.bidder_id`); upwork→agency queue → status `queued_for_browser` (flipped to `submitted` by the external browser worker; `on_behalf_of` falls back to the upwork account's `settings.on_behalf_of`); other platforms → 400 ("isn't automated — submit on the platform and use 'Mark as submitted'"). Missing bidder_id/on_behalf_of → 400 (set them on the Accounts page)
- `POST /api/proposals/{id}/mark-submitted` body `{channel?: string}` → item — records a BY-HAND submission (platforms with no automated channel): `approved`/`failed` → `submitted`, `submission_result` gets `{channel: channel || "manual", manual: true}`, audit row `proposal_marked_submitted`; 409 from any other status

### Search profiles (incl. boolean builder)
- `GET/POST /api/search-profiles`, `PUT/DELETE /api/search-profiles/{id}` (body = SearchProfile minus id/created_at)
- `POST /api/search-profiles/validate-boolean` body `{query}` → `{valid: boolean, error?: string, ast?: string}` — dry-run for the builder UI

### Platform accounts
- `GET/POST /api/accounts`, `PUT/DELETE /api/accounts/{id}` (body = PlatformAccount minus id/created_at). Credentials never pass through here — `credential_ref` points at the vault.

### Digest
- `POST /api/alerts/digest/send` → `{jobs_in_digest: number, emailed: boolean}` (emails only when `SMTP_HOST` etc. are configured)

## Scoring v2 (0–100)
keyword_match 25 (primary exact weighted 15 + secondary fuzzy 10) · budget_realism 25 (budget vs. estimated hours × market rate from rate card, $50/h fallback) · client_verification 20 (payment +9 / identity +5 / past hires ≤6) · description_quality 20 (>100 words +6, deliverables +8, tech reqs +6, vague −20) · urgency_ratio 10 (budget ÷ complexity ÷ timeline days) · red flags −30 each (cap −60): "unlimited revisions", "test task before hire", "work for exposure/review", "no upfront/milestone", "urgent + low budget", "student/budget project", equity-only. Negative keyword → score 0 + exclusion. Below filter threshold (default 40) → auto-archive.

## Auto-match pipeline
`POST /api/jobs/ingest` now: score (with rate-card market rate) → dedupe → auto-archive below threshold → `job_ingested` WS event → if any auto-queue search profile's boolean query matches (or no profiles exist) → draft proposal from platform template + rate card + auto-selected portfolio (tags fuzzy-matched to job skills, top 3) → `proposal_queue` row (`pending_review`) → `proposal_queued` WS event → alert/hot-job events as before.

---

# Contract Addendum v3 — Proposal Generation, Anti-Detection, Gig/Seller Mode (2026-08-08)

## Changed
- `ProposalQueueItem` gains: `humanized_text`, `bid_rationale`, `portfolio_match` ({itemId: {title, overlap_pct, matched_skills[]}}), `analysis` (required_skills[], deliverables[], client_pain_points[], tone, missing_info[], red_flags[], strengths[], gaps[]), `confidence` (0-100), `needs_review` (true when confidence < 50), `versions` ([{text, bid, by, at}]), `rejection_reason`, `outcome` ('pending'|'hired'|'rejected'|'ghosted'), `request_type` ('job'|'buyer_request'). New status: `generation_failed`.
- New WS message: `{type: 'generation_failed', proposal_id, error, job_id}`.
- `POST /api/proposals/{id}/reject` body changed to `{reviewer, reason: 'too_generic'|'too_expensive'|'wrong_tone'|'overpromising'|'other', notes}` — feeds rejection learning.

## New endpoints

### Proposals (enhanced)
- `POST /api/proposals/bulk-approve` body `{ids: number[], reviewer}` → `{approved, skipped}`
- `POST /api/proposals/{id}/outcome` body `{outcome: 'hired'|'rejected'|'ghosted'}` → item (updates template win_rate)
- `POST /api/proposals/{id}/revert` body `{version_index}` → item (restores text/bid from `versions`)
- `GET /api/proposals/templates/suggest?platform=upwork&skills=react,typescript` → `Template[]` (top-3 by win rate + skill overlap; Template = {id, title, platform, text, bid, tags[], uses, wins, losses, win_rate, created_at})
- `POST /api/proposals/templates/generate` body `{platform, skills[], tone?, save?, temperature?, max_tokens?, timeout?}` → `{text, model, provider, latency_ms, offline, warning?, saved?}` — LLM-drafts a reusable Template (offline fallback when the provider is down); `save=true` persists it to the library
- Approving now auto-saves the proposal as a Template (learning loop).

### Gigs (`/api/gigs`)
- `GET /api/gigs/taxonomy/fiverr` → `{categories: {cat: [subcats]}}` (seed dataset)
- `POST /api/gigs/seo-title-score` body `{title, keywords[]}` → `{score: 0-100, issues[]}`
- `POST /api/gigs/faqs/generate` body `{gig_type, title, count}` → `{faqs: [{question, answer}]}`
- `GET/POST /api/gigs/templates`, `PUT/DELETE /api/gigs/templates/{id}` — body `{platform, name, template_json, auto_publish}`; 422 `{detail: {validation: string[]}}` on invalid Fiverr/Upwork templates
- `POST /api/gigs/templates/{id}/toggle` — activate/deactivate
- `POST /api/gigs/templates/{id}/create-gig` → `{stealth_task_id, status, note}` (Fiverr = always DRAFT; Upwork respects template auto_publish). 429 when circuit open or rate-limited (1 draft/hour/platform)
- `GET/POST /api/gigs` — list/register tracked gigs `{platform, title, external_id, url, status: draft|active|paused, price_min, template_id}`
- `GET /api/gigs/metrics?gig_id=` → metric series `{week, impressions, clicks, orders, revenue, suggestions[]}`; `POST /api/gigs/metrics` (stealth worker posts scrapes); `POST /api/gigs/metrics/scrape` (enqueue weekly scrape)
- `GET/POST /api/gigs/competitors?platform=&category=` — competitor snapshots `{gigs[], insights[]}`
- `GET /api/gigs/buyer-requests` → `{offers_remaining_today, daily_limit: 10, count}`; `POST /api/gigs/buyer-requests/process` body `{requests[]}` (stealth worker posts scrapes; matching offers auto-queued as request_type='buyer_request', always pending_review)
- `GET /api/gigs/stealth-tasks?platform=&status=pending` — browser worker poll; `POST /api/gigs/stealth-tasks/{id}/claim` body `{worker_id}` → task — atomic claim (**404** unknown, **409** already claimed/done); `POST /api/gigs/stealth-tasks/{id}/complete` body `{success, result, worker_id}` — completion REQUIRES the claiming `worker_id` (`claimed_by` binding: **409** when another worker completes, or when the task isn't `claimed`) (3+ failures in the last hour trip the circuit breaker)
- `GET/POST /api/gigs/circuit/{platform}` — read/open/close the per-platform circuit breaker

## Generation pipeline (server-side, for UI display)
`POST /api/jobs/ingest` → orchestrator → `proposal_gen.generate()`: job analysis (LLM JSON or heuristic fallback) → portfolio/skill-gap match → platform-tuned draft (per-platform system prompts; offline deterministic composer when `LLM_API_KEY` unset) → anti-detection pass (banned phrases, AI-tell strip, list flattening, personality marker, typing plan) → bid calc (est. hours × rate-card × complexity 1.0–1.5) → confidence score. Failures → queue item with status `generation_failed` + WS alert. Approvals save Templates; outcomes update win rates; rejections tune future temperature/prompts per platform.

## New env vars
`LLM_API_KEY`, `LLM_BASE_URL` (default OpenAI), `LLM_MODEL` (default gpt-4o-mini), `LLM_MAX_RPM` (default 60). Celery: `celery -A app.tasks worker` + `celery -A app.tasks beat` (buyer-request monitor every 15 min, gig analytics weekly Mon 06:12 UTC).

---

# Contract Addendum v4 — Learning Loop (Phase 2, 2026-08-29)

## Changed
- `ProposalQueueItem` gains `client_replied_at: string|null` (ISO datetime of the first
  client message detected after submission).
- **Generation is off the request path**: `POST /api/jobs/ingest` (and adapter search /
  discovery) now return after scoring + persistence; proposal drafting runs as a Celery
  task. A qualifying job no longer produces a `pending_review` item synchronously — the
  `proposal_queued` WS event is the signal that a draft is ready.
- Template provenance: approving an item that was started from a suggested template
  (`template_id` set) increments THAT template's `uses` instead of minting a new Template.
  `uses` is also incremented at selection time (few-shot injection / suggestions endpoint).

## New endpoints

### Search profiles
- `POST /api/search-profiles/{id}/run-now` → `{queued: true, platforms: string[]}` —
  runs discovery for the profile's platforms immediately (adapter search + ingest inline;
  proposal generation enqueued as tasks). `platforms` lists the platforms actually searched.

### Jobs
- `POST /api/jobs/bulk-archive` body `{ids: number[]}` → `{archived: number[], skipped: number[]}`
  (tenant-scoped; unknown/foreign/already-archived ids land in `skipped`).

### Analytics (learning loop)
- `GET /api/analytics/funnel` →
  ```ts
  {
    funnel: { queued: number; approved: number; submitted: number; replied: number;
              hired: number; rejected: number; ghosted: number };
    by_platform: [{ platform: string; queued: number; approved: number; submitted: number;
                    replied: number; hired: number; win_rate: number|null }];
    by_template: [{ template_id: number; title: string; platform: string;
                    uses: number; wins: number; losses: number; win_rate: number|null }];
    by_bid_band: [{ band: string; submitted: number; hired: number; win_rate: number|null }];
    rejection_reasons: [{ reason: string; count: number }];
  }
  ```
  `win_rate` = wins/(wins+losses) as a 0–100 float (one decimal), or `null` when there are no outcomes yet — same scale as `Template.win_rate`.
  Bid bands: `<100`, `100-500`, `500-1000`, `1000+` (USD). All counts tenant-scoped, aggregated in SQL.

## New WS message
- `{type: 'client_replied', proposal_id: number, job_id: number, snippet: string}` —
  pushed when the outcome-sync beat detects a client message newer than the submission.

## Server-side beats (informational)
Celery beat schedule additions: discovery every 15 min (per enabled search profile,
per-platform pacing), Freelancer outcome/reply sync every 30 min, generation_failed
retry every 30 min (max 2 retries, 24h window), digests hourly (honoring `digest_mode`;
daily sends at 07:00 UTC; the tick fans out one `digest_user_task` per due user so a
blocking SMTP send can't delay other tenants), stale-job auto-archive daily (deadline
passed or fetched >14d).

---

# Contract Addendum v5 — Winning-Advantage Features (Phase 3, 2026-08-29)

## Changed
- `request_type` gains `'follow_up'` (now `'job' | 'buyer_request' | 'follow_up'`).
- `ProposalQueueItem` gains `bid_advice: {recommendation: 'bid'|'caution'|'skip', reason: string} | null`
  — go/no-go market intel computed at queue time from the job's proposals count + quality
  score (`skip` when >25 proposals AND score <70; `caution` when >15; `bid` otherwise;
  `null` when the platform reports no proposals count).
- `JobOut` gains `client_history: {past_proposals, hired, rejected, ghosted} | null`,
  populated on `GET /api/jobs/{id}` only. Aggregates the tenant's past proposals for the
  same client; `null` when the client was never seen (or cannot be identified).
- `ClientInfo` gains `client_id: string | null` and `name: string | null`. Adapters now
  also populate `past_hires` (Upwork `totalHires`), `identity_verified` (Freelancer owner
  `status.identity_verified`), and `client_id`/`name` where the API exposes them — this
  feeds the client_verification score component and client identity.

## Client identity
A job's client is identified by `(platform, client_id)` when the adapter supplies one,
else `(platform, name)`, else a `(platform, country, rating, total_spent bucket)`
composite. Buckets: `0`, `<1k`, `1-10k`, `10-50k`, `50k+`. Proposals on the job itself
are excluded from its own `client_history`.

## New endpoints

### Proposals
- `POST /api/proposals/{id}/follow-up` → `ProposalQueueItem` — drafts a follow-up message
  for an item in `submitted`/`queued_for_browser` whose `outcome` is still `pending`.
  Creates a NEW queue item: `status='pending_review'`, `request_type='follow_up'`, same
  `job_id`/`platform`, `submission_result.parent_proposal_id` = the original id. It flows
  through the normal review boundary (approve → submit). 409 when the original isn't
  submitted-ish, the outcome is terminal, or a follow-up for it is already
  pending_review/approved.
- `GET /api/proposals/{id}/interview-prep` →
  ```ts
  { questions: [{question: string, suggested_answer: string}];   // 5, grounded in portfolio
    pain_points: string[]; red_flags: string[]; talking_points: string[] }
  ```
  Generated from the item's stored analysis + matched portfolio (LLM with deterministic
  offline fallback). Cached on the item (`submission_result.interview_prep`) after the
  first call — repeated GETs are free.
- `POST /api/proposals/{id}/retry-generation` → `ProposalQueueItem` — re-runs generation
  for a `generation_failed` item: resets the auto-retry budget
  (`submission_result.generation_retries`) and enqueues the job's generation task, which
  regenerates THIS row in place. **409** unless `generation_failed`; **503** when the
  task broker is down (the auto-retry beat still picks it up).

## Server-side behavior (informational)
- Client history, when it exists, is injected into the proposal-generation prompt as
  operator context (outside the untrusted `<job_posting>` tags): "you've bid Nx for this
  client: X hired, Y rejected, Z ghosted".
- Won-bid learning: marking an outcome `hired` records the winning `bid_amount` under
  AdapterState key `rate_feedback:{skill_category}`. Once a category has ≥3 samples,
  `calculate_bid` nudges fixed-price estimates toward the historical winning average,
  bounded to ±20% of the computed estimate (noted in `bid_rationale`).

---

# Contract Addendum v6 — Credential Enrollment (2026-08-29)

Users can now connect platform accounts through the product: secrets are
written into the Fernet vault under the account's `credential_ref`
(auto-generated as `vault://{platform}/{principal}` when the account has
none; unique per `(user, platform, principal)` — the same triple the vault
keys on). Secret values are **never** returned by any endpoint and never
logged; enroll/delete write an `AuditLog` row (`credentials_enrolled` /
`credentials_deleted`) carrying key names only.

## New endpoints (user JWT, tenant-scoped via the account)

### Credentials
- `POST /api/accounts/{id}/credentials` body `{secrets: Record<string,string>}`
  → **204**. Validates keys per platform, stores the dict in the vault, and
  persists `credential_ref` on the account when missing. **422** on: empty
  dict, unknown keys, missing required keys, both credential forms at once,
  or invalid `storage_state_json`. Recognized keys:
  - `freelancer` / `upwork`: `access_token` (required), `refresh_token` (optional)
  - stealth platforms (`fiverr`, `peopleperhour`, `guru`, `linkedin`, `indeed`):
    `storage_state_json` (a Playwright `storage_state` JSON string — preferred)
    **or** `username` + `password` together. Password-based login is a
    fallback-only path for the worker's login flow — it is challenge-prone
    (CAPTCHA/2FA) and may escalate to a human.
- `GET /api/accounts/{id}/credentials/status` →
  `{enrolled: bool, keys: string[], updated_at: string|null}` — key names
  only, never values.
- `DELETE /api/accounts/{id}/credentials` → **204** — removes the vault row
  and clears `credential_ref` on the account.

### Freelancer OAuth 2.0
- `GET /api/accounts/{id}/oauth/freelancer/start` → `{authorize_url: string}`.
  **501** when `FREELANCER_CLIENT_ID`/`FREELANCER_CLIENT_SECRET` are not
  configured on the deployment (enroll tokens manually instead). **400** when
  the account is not a freelancer account.
- `POST /api/accounts/{id}/oauth/freelancer/complete` body
  `{code: string, redirect_uri?: string}` → **204** — exchanges the code and
  stores the tokens in the vault under the account's principal.
  `redirect_uri` defaults to `FREELANCER_REDIRECT_URI`.

## New endpoint (worker token only)
- `GET /api/gigs/stealth-session?platform=X&user_id=N` →
  `{storage_state: object|null, credentials_present: bool}` — the enrolled
  Playwright `storage_state` for that tenant/platform so the browser worker
  can seed its context without the CLI login flow. `storage_state` is `null`
  when nothing (or only username/password) is enrolled. Tenancy comes from
  the explicit `user_id` (the worker pool serves all tenants, same as
  stealth-task polling); user JWTs get **401**.

## Worker behavior change
The worker prefers the API-provided session: when launching a browser
context for `(platform, user_id)` it fetches `/api/gigs/stealth-session`
and applies the returned `storage_state` (cookies + localStorage). When
absent or unreachable it falls back to the local persistent profile under
`WORKER_SESSION_DIR` (the `worker.login` CLI flow keeps working).

---

# Contract Addendum v7 — Outcome Sync via Worker, Follow-up Automation, Trend (2026-08-29)

## New endpoints

### Analytics
- `GET /api/analytics/trend?weeks=8` (user JWT, tenant-scoped) →
  ```ts
  { weeks: [{ week: string;            // ISO week label, e.g. "2026-W35"
              submitted: number;
              replied: number;
              hired: number;
              win_rate: number | null  // 0–100; null when the week has no outcomes
            }] }                       // oldest first; `weeks` param 1–26 (default 8)
  ```
  Aggregation is by day in SQL, bucketed into ISO weeks server-side.
  Timestamps: `submitted` uses coalesce(`reviewed_at`, `created_at`)
  (`reviewed_at` is the submission proxy — set at approval, the last step
  before submission); `replied` uses `client_replied_at`; `hired`/losses use
  the `outcome_recorded_at` stamp that `record_outcome` now writes into
  `submission_result` (legacy rows without the stamp fall back to
  `created_at`). `win_rate` = hired / (hired + rejected + ghosted) per week.

### Gigs (worker token only)
- `POST /api/gigs/proposal-status` body
  `{task_id: number, results: [{proposal_queue_item_id: number,
  platform_status: 'pending'|'viewed'|'interviewing'|'hired'|'declined',
  has_unread_reply: boolean}]}` →
  `{task_id, task_status, outcomes, replies, skipped}`. Result-application of
  a `scrape_proposal_status` stealth task: `hired` → outcome `hired`,
  `declined` → outcome `rejected` (via the same `record_outcome` path as
  Freelancer sync, so template win rates update); `has_unread_reply` → sets
  `client_replied_at` and broadcasts the `client_replied` WS event.
  Idempotent (reposts are no-ops), tenant-checked per row (foreign or unknown
  items are counted in `skipped`), and completes the stealth task. User JWTs
  get **401**; unknown task/kind → **404**; malformed body → **422**.

## Changed
- `GET /api/proposals` now refreshes `bid_advice` for returned-page items
  older than 24h: recomputed from the job's CURRENT `proposals_count` ×
  `quality_score` and persisted when changed. Fresh items keep their
  queue-time advice. Response shape unchanged.
- `record_outcome` (all callers: manual outcome marking, Freelancer sync,
  worker proposal-status) stamps `submission_result.outcome_recorded_at`
  once — first stamp wins. No schema change.

## Server-side behavior (informational)
- **Platform outcome/reply sync (worker-driven, generalized).** Stealth task
  kind `scrape_proposal_status` (read-only). Beat `upwork_outcome_tick` every
  60 min (task name kept stable for the beat schedule) fans out per active
  user: tenants with an enabled PlatformAccount on ANY browser platform
  (upwork, fiverr, peopleperhour, guru) AND proposal_queue items in
  `submitted`/`queued_for_browser` on that platform get one task per platform
  whose payload lists `{proposal_queue_item_id, job_external_id, job_url}`
  per item. At most one such task is in flight per tenant+platform (no
  stacking; one platform's in-flight scrape doesn't block the others). The
  worker scrapes the platform's proposals/inbox listing (Upwork proposals,
  Fiverr seller inbox incl. brief responses, PeoplePerHour WorkStream, Guru
  quotes — best-effort selectors in `worker/platforms.py`) and posts back to
  the endpoint above. Result application is platform-agnostic: per-row
  tenancy checks and idempotency hold for all four platforms.
- **Follow-up due automation.** Beat `follow_up_due_tick` daily 09:05 UTC,
  fanned out per active user. Eligible items: `request_type='job'`,
  `status='submitted'` (Upwork items stay `queued_for_browser` until the
  browser worker confirms, so only truly-submitted items qualify),
  `outcome='pending'`, `client_replied_at IS NULL`, submission proxy
  coalesce(`reviewed_at`, `created_at`) older than 5 days, and NO existing
  follow-up child (any status — the automation never re-nags after a human
  rejected one). Auto-drafts via the same `generate_follow_up` pipeline as
  the manual endpoint and parks them `pending_review` (human boundary
  unchanged), capped at 5 per user per run; per-item failures are logged and
  skipped. Each queued follow-up emits the existing `proposal_queued` WS
  event, carries `submission_result.{parent_proposal_id, auto: true}`, and
  writes a `follow_up_generated` AuditLog row with `auto: true`.

## New WS message
None — the automation reuses `proposal_queued` and `client_replied`.

---

# Contract Addendum v8 — Account Lifecycle & Data Retention (2026-08-29)

## New endpoints (user JWT, tenant-scoped)

### Auth
- `POST /api/auth/password` body `{current_password, new_password}` →
  `{status: "ok"}`. Verifies the current password (**400** on mismatch),
  enforces min length 8 / max 72 on the new one (**422** otherwise), and
  updates the hash. Existing tokens stay valid (stateless JWTs) — the client
  keeps its session and uses the new password at the next login.
- `DELETE /api/auth/account` body `{password}` → `{status: "deleted"}`.
  Verifies the password (**400** on mismatch), then deletes the user row;
  every tenant table's `user_id` FK is `ON DELETE CASCADE`, so all tenant
  data goes with it. No final `AuditLog` row is written (audit_log.user_id is
  a non-nullable cascading FK — the row would be erased by the very delete it
  records); an application-log line is emitted instead. The old token
  immediately stops resolving (**401** on subsequent calls).

### Frontend
The SPA user menu gains **Change password** (modal: current + new + confirm)
and **Delete account** (typed-confirm: the account email + password) flows
calling the endpoints above; successful deletion drops the session back to
Login.

## Server-side behavior (informational)
- **Data retention sweep.** New daily beat `retention_tick` (04:11 UTC),
  per tenant:
  - hard-deletes jobs with `status='archived'` and `fetched_at` older than
    90 days — except any still referenced by the proposal queue (archived
    jobs normally have none; referenced ones are skipped and counted);
  - hard-deletes `done`/`failed` stealth_tasks completed (falling back to
    created when no completion stamp) more than 30 days ago;
  - hard-deletes audit_log rows older than 365 days.
  Returns/logs per-category counts
  `{jobs_deleted, jobs_skipped_referenced, stealth_tasks_deleted,
  audit_log_deleted}`.
