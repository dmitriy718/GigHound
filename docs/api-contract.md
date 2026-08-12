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

### Alerts
- `GET /api/alerts/settings` → `AlertSettings` (singleton, auto-created)
- `PUT /api/alerts/settings` body = `AlertSettings` → `AlertSettings`
- `GET /api/alerts/digest-preview` → `{jobs: Job[]}` (what the next digest would contain)

### WebSocket
- `WS /ws/alerts` — server pushes JSON messages:
  - `{type: 'job_alert', job: Job}` — high-match job
  - `{type: 'hot_job', job: Job}` — urgency alert
  - `{type: 'digest', jobs: Job[]}` — digest payload
  Client should send `ping` text every 30s; server tolerates it.

### Profile management
- `GET /api/profiles/templates?platform=upwork` → `ProfileTemplate[]`
- `POST /api/profiles/templates` body `{platform, name, pitch_template}` → `ProfileTemplate`
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
}
type ProposalStatus = 'pending_review'|'approved'|'rejected'|'submitted'|'failed';
interface ProposalQueueItem {
  id: number; job_id: number; platform: Platform;
  proposal_text: string; bid_amount: number|null; bid_period_days: number|null;
  portfolio_item_ids: number[]; template_id: number|null;
  status: ProposalStatus; reviewed_by: string|null; reviewed_at: string|null;
  submission_result: Record<string, unknown>; created_at: string;
  job?: Job;                          // embedded on responses
}
interface ProposalReviewAction { reviewer: string; proposal_text?: string; bid_amount?: number; bid_period_days?: number; }
```

## New endpoints

### Proposal review queue (human-in-the-loop boundary)
- `GET /api/proposals?status=pending_review` → `ProposalQueueItem[]`
- `GET /api/proposals/{id}` → `ProposalQueueItem`
- `POST /api/proposals/{id}/approve` body `ProposalReviewAction` → item (reviewer may edit text/bid before approving; 409 unless pending_review)
- `POST /api/proposals/{id}/reject` body `{reviewer}` → item
- `POST /api/proposals/{id}/submit` → item — dispatches via compliant channel: freelancer→official bid API (needs `submission_result.bidder_id`), upwork→agency queue; other platforms → 501 (stealth worker's job)

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
- `GET /api/gigs/stealth-tasks?platform=&status=pending` — browser worker poll; `POST /api/gigs/stealth-tasks/{id}/complete` body `{success, result}` (3+ failures trip the circuit breaker)
- `GET/POST /api/gigs/circuit/{platform}` — read/open/close the per-platform circuit breaker

## Generation pipeline (server-side, for UI display)
`POST /api/jobs/ingest` → orchestrator → `proposal_gen.generate()`: job analysis (LLM JSON or heuristic fallback) → portfolio/skill-gap match → platform-tuned draft (per-platform system prompts; offline deterministic composer when `LLM_API_KEY` unset) → anti-detection pass (banned phrases, AI-tell strip, list flattening, personality marker, typing plan) → bid calc (est. hours × rate-card × complexity 1.0–1.5) → confidence score. Failures → queue item with status `generation_failed` + WS alert. Approvals save Templates; outcomes update win rates; rejections tune future temperature/prompts per platform.

## New env vars
`LLM_API_KEY`, `LLM_BASE_URL` (default OpenAI), `LLM_MODEL` (default gpt-4o-mini), `LLM_MAX_RPM` (default 60). Celery: `celery -A app.tasks worker` + `celery -A app.tasks beat` (buyer-request monitor every 15 min, gig analytics weekly Mon 06:12 UTC).
