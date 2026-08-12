# GigHound — Platform Intelligence Report

**Date:** 2026-08-08 · **Scope:** Upwork, Fiverr, Freelancer.com, PeoplePerHour, Guru.com, LinkedIn Jobs, Indeed
**Purpose:** Foundational research for an autonomous multi-platform freelance job application & gig management system. Findings verified against live sources and direct HTTP probes where noted.

---

## 1. Platform Matrix

| Platform | API Availability | Application Method | Anti-Bot Stack | Compliance Loophole | Risk Level | Gig Mode |
|---|---|---|---|---|---|---|
| **Upwork** | Official **GraphQL API** (`api.upwork.com/graphql`), OAuth 2.0, keys granted after manual use-case review, **10 req/s per IP**. Job search: **YES** (`marketplaceJobPostingsSearch`). Proposal submission: **NO** — no mutation exists to submit proposals, answer screening questions, or spend Connects. | **Hybrid** — API for discovery; browser/session automation or agency route for submission. Pure-API submission impossible. | **Cloudflare** (verified live: `cf-mitigated: challenge`, `__cf_bm`, JS challenge) → TLS/JA3, HTTP/2 fingerprinting, Turnstile CAPTCHA, IP reputation. Server-side ML: linguistic AI-pattern detection, timing anomalies, structural fingerprints, cross-account similarity, behavioral metrics. | **Agency Plus / Business Manager** — agency BMs can officially submit proposals on behalf of any agency member. The only quasi-legitimate scaled submission path. Approved API keys permit compliant job-alert tooling. **Human-in-the-loop review mandatory per Upwork's 2026 AI policy.** | **High** (Extreme for auto-submit bots). ToS bans scraping/unapproved automation; tiered enforcement up to permanent ban without appeal. | **Both** — apply to jobs + post pre-packaged services via Project Catalog (up to 20 projects). |
| **Fiverr** | **No general public API.** Affiliate API exists (Cellxpert/Awin/Skimlinks) but is **read-only**, marketing-scoped. No job search API, no application API. Buyer Requests removed Oct 2022 → replaced by **Fiverr Briefs** (algorithm-matched, no open bidding surface). | **Browser automation only** — and low-value: no bid surface to automate; only gig management + Brief replies. | **PerimeterX (HUMAN Security) behind Cloudflare** (verified live: `403`, `fvrr-bl-route-id: px`). Press-and-hold CAPTCHA, behavioral biometrics (mouse/scroll/keystroke ML), `_px3` token, 100+ fingerprint signals, IP reputation. Defeats even `curl_cffi` TLS impersonation. | **Weak.** Affiliate API is the only sanctioned programmatic access. Fiverr Briefs matching is Fiverr-controlled, not user-drivable. | **High.** ToS bans robots/scrapers; enforcement: warnings → 60-day restriction → permanent suspension. | **Both, asymmetric** — primary seller mode (post gigs); buyers send Briefs, sellers respond to private matches. No open job-application mode. |
| **Freelancer.com** | **Official public API** (developers.freelancer.com, BETA), OAuth 2.0, base `https://www.freelancer.com/api/0.1`, official Python SDK + sandbox. Job search: **YES** (`GET /projects/0.1/projects/active`). Bid submission: **YES, verified** — `POST /projects/0.1/bids` (create/award/accept/retract/highlight). Also users, messages, milestones, contests. Rate limits undocumented; handle 429s. | **Official API (fully sufficient)** — search + bid are first-class. Hybrid only for UI-only features. | **Cloudflare** (JS challenges, bot scoring, TLS fingerprinting) — relevant only to web scraping; OAuth API access bypasses it entirely. | **The official API IS the loophole** — Freelancer actively markets automated bidding ("scale with a few lines of code"). Developer approval required; sandbox provided. | **Low (via API) / High (via browser)**. User Agreement bans robots/scrapers on the Website; sanctioned OAuth bidding is contractually fine. | **Both** — bid on projects/contests, post projects as employer, and sell fixed-price Services (Fiverr-style, unlimited listings). |
| **PeoplePerHour** | **No official API** — `/api`, `/developers` return 404; no developer portal or OAuth (verified 2026-08-08). Only third-party scrapers (Apify). No API job search, no API proposal submission. | **Browser automation** — proposals only via web UI (WorkStream flow). Scraping viable for discovery. | **AWS CloudFront + nginx/Express** — no Cloudflare/DataDome/PerimeterX observed. No documented JA3/ML bot detection. Internal ML exists for proposal *ranking*, not bot detection. `robots.txt` disallows `/job/new*` and `/job/bidders*` (proposal endpoints). Public pages lightly defended; logged-in checks undocumented. | **Weak.** No bid API, no agency feature. Proposals are a scarce paid resource (Basic: 15 credits/mo; TopAccess: +10 +1 featured) — volume is economically capped by design. Public listings crawlable (sitemaps published). | **High.** TOS allows indefinite blocking for breaching the letter *or spirit* of terms; identity-verified accounts hold escrowed funds — bans freeze balances. | **Both** — post fixed-price "Offers" (gigs) and bid on buyer-posted projects. |
| **Guru.com** | **No official public API** (note: `developer.getguru.com` is a different company). No OAuth, no endpoints for search or quote submission. Legacy RSS job feed now **404** (verified Aug 2026). | **Browser automation only** — "Send a Quote" via web UI. Third-party aggregators (Vollna, BidPacer) for discovery. | **Imperva Incapsula** (verified live: `x-iinfo` header, `visid_incap_*`/`incap_ses_*` cookies, JS-challenge iframe on every page incl. robots.txt). Stack: JS challenges, device fingerprinting, datacenter IP blocking, behavioral analysis, CAPTCHA escalation; reCAPTCHA on forms per third-party reports. | **Weak.** Quote Templates (official reusable proposals), membership-tier bid quotas (Basic→Executive, extra bids purchasable), job-match email alerts. No partner write API or agency channel. | **High.** TOS: suspension/termination at "sole discretion", prosecution "to the fullest extent of the law". Active Incapsula enforcement. | **Both** — freelancers advertise services + submit Quotes; employers post jobs + request quotes directly. |
| **LinkedIn Jobs** | **No open-access Jobs API.** All official jobs APIs behind the **Talent Solutions partner program** (incorporated companies, signed agreement, approval). `POST /v2/simpleJobPostings` + Apply Connect are **employer/ATS-side** — LinkedIn delivers applications *to* partners via webhook; no candidate-side submission API exists. Job search API: deprecated/closed. Partner rate limits ~500 calls/user/day. | **Browser automation** for Easy Apply submission (no write API for candidates). Discovery can be hybrid via third-party scraper APIs. | **In-house stack**: IP rate-limiting (429), authwall, `li_at` session tracking, behavioral/timing analysis, litigation-backed enforcement (hiQ case, $500K judgment). Automation-tool detection reportedly +340% (2023–2025), ~23% 90-day restriction rate. | **Easy Apply itself** (one-click, on-platform, high-volume) + **Services Pages** ("Open to Business") for listing freelance offerings. Apply Connect is employer-side only. | **High/Extreme.** UA §8.2 explicitly bans bots/scrapers/automation; permanent bans + CFAA litigation exposure. | **Both, asymmetric** — apply to jobs (primary); post services via Services Pages; employers post jobs. Not a bid marketplace. |
| **Indeed** | **Job search API: NO** (Publisher/Job Search APIs deprecated 2023). **Job Sync API** (GraphQL, `apis.indeed.com/graphql`) is **employer/ATS-only** (job posting, ~150 req/min). **Indeed Apply API exists but partner-only** (signed MSA; delivers applicants *to* ATSs — not a job-seeker submission API). No public job-seeker OAuth scopes. | **Browser automation only** — no official job-seeker path for search or apply. | **Cloudflare** (2026) + historically **DataDome**: slider CAPTCHA, TLS/JA3–JA4 fingerprinting, HTTP/2 frame ordering, canvas/WebGL fingerprinting, mouse/keyboard behavioral analysis, IP reputation, sub-2ms ML risk scoring. 403 interstitials, IP bans, honeypots. | **None for job seekers.** Only legitimate scaled routes are employer/ATS-side partnerships (MSA for Job Sync / Indeed Apply intake). | **High–Extreme.** TOS §A.3.5 *explicitly names* banning "automation, scripting, or bots to automate the Indeed Apply process" — a named violation with active enforcement. | **Apply-only (seeker) / post-jobs (employer)** — not a freelance marketplace; no seller gig mode. |

### Key corrections to initial assumptions
- **Upwork API exists but is read-only for the application loop** — job search yes, proposal submission no (confirmed against the live schema).
- **Guru.com is behind Imperva Incapsula**, not Cloudflare as commonly claimed; its RSS job feed is dead.
- **PeoplePerHour has the lightest perimeter defenses** (CloudFront, no commercial anti-bot vendor observed) but the harshest account-level economics (paid proposal credits, identity verification, escrowed funds at risk).
- **Indeed's Job Search API is gone (2023)** — even discovery requires scraping or partner status.
- **Fiverr has no bidding surface at all anymore** (Briefs replaced Buyer Requests) — it is a gig-management target, not an application target.

---

## 2. Strategic Summary

| Tier | Platforms | Approach |
|---|---|---|
| **Tier 1 — API-native** | Freelancer.com | Full loop via official OAuth API. Lowest risk, fastest to ship. Build first. |
| **Tier 2 — Hybrid** | Upwork | Official GraphQL API for discovery; Agency Plus Business Manager channel or HITL browser automation for submission. Highest revenue potential, highest policy scrutiny. |
| **Tier 3 — Stealth browser** | PeoplePerHour, Guru.com, LinkedIn, Indeed | No write APIs. Stealth browser adapters + human review queue + human-scale pacing. LinkedIn/Indeed carry Extreme risk — consider manual-approval-only mode. |
| **Tier 4 — Gig mode only** | Fiverr | No application surface. Use for gig/service listing management and Brief responses only. |

**Non-negotiable design constraint:** Upwork's 2026 AI policy and every platform's ToS point the same way — **no submission leaves the system without human approval**. The Human Review Queue is a compliance boundary, not a UX nicety.

---

## 3. Recommended Architecture

```
                              ┌─────────────────────────────────────────────┐
                              │                 DASHBOARD (Web UI)           │
                              │  job feed · approvals · analytics · settings │
                              └───────────────────┬─────────────────────────┘
                                                  │ REST/WebSocket
                              ┌───────────────────▼─────────────────────────┐
                              │        DASHBOARD API (FastAPI / Express)     │
                              │   authn/z · job CRUD · review actions ·      │
                              │   platform account management · audit log    │
                              └───────────────────┬─────────────────────────┘
                                                  │
┌─────────────────────────────────────────────────▼────────────────────────────────────────┐
│                          CENTRAL ORCHESTRATOR (Node.js / Python)                          │
│                                                                                           │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  ┌────────────────────────────┐   │
│  │ Job Discovery │  │  Matching &  │  │  Proposal/    │  │   Scheduler & Rate-Limit    │   │
│  │  Scheduler   │─▶│  Scoring     │─▶│  Cover-Letter │─▶│   Governor (per-platform    │   │
│  │ (poll APIs / │  │  (LLM fit    │  │  Generator    │  │   budgets, human-scale      │   │
│  │  scrapers)   │  │  analysis)   │  │  (LLM draft)  │  │   pacing, jitter)           │   │
│  └──────────────┘  └──────────────┘  └───────────────┘  └────────────────────────────┘   │
└───────┬───────────────────────┬──────────────────────────────┬─────────────────────────────┘
        │ discovered jobs       │ scored matches               │ draft proposals
        ▼                       ▼                              ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│                          HUMAN REVIEW QUEUE  ★ COMPLIANCE BOUNDARY ★                       │
│   Every submission (bid/proposal/Easy Apply) parks here. Approve / Edit / Reject.          │
│   Nothing auto-submits. Full audit trail (who approved, what changed, when).               │
│   Satisfies: Upwork 2026 AI policy (HITL mandatory) · platform ToS · quality control.      │
└───────────────────────────────────────────┬───────────────────────────────────────────────┘
                                            │ approved submissions only
                    ┌───────────────────────▼────────────────────────┐
                    │           PLATFORM ADAPTER LAYER                │
                    │  (common interface: search / apply / sync /     │
                    │   postGig / message — per-platform impls)       │
                    └───────┬───────────────────────────┬────────────┘
                            │                           │
        ┌───────────────────▼─────────────┐ ┌───────────▼─────────────────────────┐
        │   OFFICIAL API ADAPTERS           │ │   STEALTH BROWSER ADAPTERS          │
        │  ┌─────────────────────────────┐ │ │  ┌───────────────────────────────┐  │
        │  │ Freelancer.com  ── full loop│ │ │  │ Upwork     (submit-only, or   │  │
        │  │   (search + bid via         │ │ │  │            Agency Plus BM     │  │
        │  │   /projects/0.1/bids)       │ │ │  │            channel)           │  │
        │  │ Upwork GraphQL ── discovery │ │ │  │ PeoplePerHour / Guru          │  │
        │  │   only                      │ │ │  │ LinkedIn Easy Apply           │  │
        │  │ (future: any platform that  │ │ │  │ Indeed Apply                  │  │
        │  │  opens a write API)         │ │ │  │ Fiverr     (gig mgmt + Briefs)│  │
        │  └─────────────────────────────┘ │ │  └───────────────────────────────┘  │
        │   OAuth 2.0 · token refresh      │ │   Playwright/Puppeteer + stealth    │
        │   429 backoff · sandbox support  │ │   fingerprint rotation · JS render  │
        └──────────────────────────────────┘ │   CAPTCHA escalate-to-human         │
                                             └───────────────┬─────────────────────┘
                                                             │ stealth traffic only
                                             ┌───────────────▼─────────────────────┐
                                             │      PROXY / ROTATION POOL          │
                                             │  residential proxies · per-platform │
                                             │  sticky sessions · geo-matching ·   │
                                             │  IP health scoring & quarantine     │
                                             └─────────────────────────────────────┘

   ┌──────────────────────────────┐        ┌──────────────────────────────────────┐
   │      CREDENTIAL VAULT        │        │         DATA STORE (Postgres)         │
   │  per-platform, per-account:  │        │  jobs · proposals · submissions ·     │
   │  OAuth tokens, session       │───────▶│  messages · audit log · per-platform  │
   │  cookies, API keys           │        │  rate-limit state · proxy health      │
   │  (AES-256-GCM, envelope      │        └──────────────────────────────────────┘
   │  encryption, e.g. Vault/KMS) │
   └──────────────────────────────┘
```

### Data flow
1. **Discovery** — Orchestrator polls Freelancer/Upwork APIs on schedule; stealth adapters scrape PPH/Guru/LinkedIn/Indeed at human-scale intervals through the proxy pool.
2. **Match & draft** — Jobs are deduplicated, scored against the user's profile/skills; LLM generates tailored proposal drafts.
3. **Review** — Drafts enter the Human Review Queue. The operator approves/edits/rejects. **No adapter can submit without a queue approval token.**
4. **Submit** — Freelancer.com goes out via official API; everything else via stealth browser adapters with pacing, jitter, and CAPTCHA-escalation-to-human.
5. **Sync** — Adapters poll for replies/status changes; state reconciled into the data store and surfaced on the dashboard.

### Adapter interface (sketch)
```ts
interface PlatformAdapter {
  searchJobs(criteria: SearchCriteria): Promise<Job[]>;
  getJobDetails(id: string): Promise<JobDetails>;
  submitApplication(jobId: string, proposal: ApprovedProposal): Promise<SubmissionResult>; // requires approval token
  syncMessages(): Promise<Message[]>;
  postGig?(gig: GigListing): Promise<GigResult>;   // seller mode: Fiverr, Upwork Catalog, PPH Offers, Freelancer Services
  healthCheck(): Promise<AdapterHealth>;
}
```

### Risk controls baked in
- **Rate-Limit Governor** enforces per-platform, per-account budgets (e.g. Upwork 10 req/s; LinkedIn daily action caps) with randomized human-like jitter.
- **Credential Vault** isolates secrets per platform; stealth adapters never touch raw cookies outside the vault boundary.
- **Proxy quarantine** — an IP that triggers a CAPTCHA/challenge is benched automatically.
- **Kill switch per platform** — disable LinkedIn/Indeed automation entirely without touching the rest.
- **Audit log** — immutable record of every approval and submission for dispute/appeal scenarios.

---

## 4. Sources (selected)

- Upwork GraphQL API docs & limits: developers.upwork.com · support.upwork.com (API limits, Agency Plus, automation policy)
- GigRadar Upwork API schema analysis (Apr 2026): gigradar.io/blog/upwork-api
- Fiverr: TOS (fiverr.com/legal-portal) · Scrapfly PerimeterX bypass analysis · Apify Fiverr scraper notes
- Freelancer.com: developers.freelancer.com · freelancer-sdk-python (GitHub) · Rollout integration guides · User Agreement (2025-06)
- PeoplePerHour: TOS & PPH Manual (peopleperhour.com/static/terms) · direct probes 2026-08-08
- Guru.com: TOS §8 · Guru Help (Send a Quote, Quote Templates) · direct Incapsula probes Aug 2026
- LinkedIn: Microsoft Learn Apply Connect docs · User Agreement §8.2 · hiQ litigation analyses
- Indeed: docs.indeed.com (Job Sync API guide, Indeed Apply) · indeed.com/legal §A.3.5 · Scrapfly Indeed scraping guide (2026)
