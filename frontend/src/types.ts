// Domain types mirrored from docs/api-contract.md

export interface User {
  id: number;
  email: string;
  display_name: string;
  is_active: boolean;
  created_at: string;
}

export type Platform =
  | 'upwork'
  | 'fiverr'
  | 'freelancer'
  | 'peopleperhour'
  | 'guru'
  | 'linkedin'
  | 'indeed';

export type KeywordKind = 'primary' | 'secondary' | 'negative';
export type JobType = 'fixed' | 'hourly' | 'retainer' | 'contest' | 'gig';
export type ExperienceLevel = 'entry' | 'intermediate' | 'expert';
export type WorkArrangement = 'remote' | 'onsite' | 'hybrid';
export type JobStatus = 'new' | 'notified' | 'archived';

export const PLATFORMS: Platform[] = [
  'upwork',
  'fiverr',
  'freelancer',
  'peopleperhour',
  'guru',
  'linkedin',
  'indeed',
];
export const JOB_TYPES: JobType[] = ['fixed', 'hourly', 'retainer', 'contest', 'gig'];
export const EXPERIENCE_LEVELS: ExperienceLevel[] = ['entry', 'intermediate', 'expert'];
export const WORK_ARRANGEMENTS: WorkArrangement[] = ['remote', 'onsite', 'hybrid'];

export interface Keyword {
  id?: number;
  term: string;
  kind: KeywordKind;
  weight: number; // 0.0–1.0 (primary); ignored for negative
}

export interface KeywordGroup {
  id: number;
  name: string;
  service_type: string;
  keywords: Keyword[];
  created_at: string;
}

export interface ClientFilters {
  payment_verified?: boolean | null;
  min_hire_rate?: number | null; // 0–100
  min_total_spent?: number | null; // USD
  countries?: string[]; // ISO codes
}

export interface PlatformBudget {
  platform: Platform;
  min?: number | null;
  max?: number | null;
  currency: string;
}

export interface SearchFilter {
  id: number;
  name: string;
  keyword_group_id?: number | null;
  platforms: Platform[];
  job_types: JobType[];
  budgets: PlatformBudget[]; // per-platform ranges, normalized to USD server-side
  experience_levels: ExperienceLevel[];
  client_filters: ClientFilters;
  posted_within_hours?: number | null;
  apply_deadline_within_hours?: number | null;
  work_arrangements: WorkArrangement[];
  languages: string[];
  max_proposals?: number | null; // skip oversaturated jobs
  quality_threshold: number; // 0–100, auto-archive below this
  created_at: string;
}

export interface ScoreBreakdown {
  [component: string]: number; // v2 keys: keyword_match, budget_realism, client_verification, description_quality, urgency_ratio, red_flag_penalty
}

export interface ClientInfo {
  payment_verified?: boolean;
  identity_verified?: boolean;
  past_hires?: number;
  hire_rate?: number;
  total_spent?: number;
  country?: string;
  rating?: number;
  reviews_count?: number;
}

// Phase 3: outcome history with this client — present on GET /api/jobs/{id}, null when never seen
export interface ClientHistory {
  past_proposals: number;
  hired: number;
  rejected: number;
  ghosted: number;
}

export interface Job {
  id: number;
  external_id: string;
  platform: Platform;
  title: string;
  description: string;
  url: string;
  job_type: JobType | null;
  budget_min: number | null;
  budget_max: number | null;
  currency: string;
  budget_usd_min: number | null;
  budget_usd_max: number | null;
  experience_level: ExperienceLevel | null;
  client_info: ClientInfo;
  proposals_count: number | null;
  skills: string[];
  languages: string[];
  work_arrangement: WorkArrangement | null;
  posted_at: string | null;
  apply_deadline: string | null;
  quality_score: number;
  score_breakdown: ScoreBreakdown;
  red_flags: string[];
  status: JobStatus;
  is_duplicate: boolean;
  duplicate_of: number | null;
  client_history?: ClientHistory | null; // Phase 3: detail endpoint only
  fetched_at: string;
}

// Job minus id/quality_score/score_breakdown/red_flags/status/is_duplicate/duplicate_of/fetched_at
export interface JobIngest {
  external_id: string;
  platform: Platform;
  title: string;
  description: string;
  url: string;
  job_type: JobType | null;
  budget_min: number | null;
  budget_max: number | null;
  currency: string;
  budget_usd_min: number | null;
  budget_usd_max: number | null;
  experience_level: ExperienceLevel | null;
  client_info: ClientInfo;
  proposals_count: number | null;
  skills: string[];
  languages: string[];
  work_arrangement: WorkArrangement | null;
  posted_at: string | null;
  apply_deadline: string | null;
}

export interface AlertSettings {
  realtime_enabled: boolean;
  min_score_alert: number; // only alert jobs ≥ this score
  digest_mode: 'off' | 'hourly' | 'daily';
  hot_job_enabled: boolean;
  hot_job_min_score: number; // "hot" = score ≥ this AND proposals < hot_job_max_proposals AND posted within hot_job_posted_hours
  hot_job_max_proposals: number;
  hot_job_posted_hours: number;
}

export interface ProfileTemplate {
  id: number;
  platform: Platform;
  name: string;
  pitch_template: string;
  created_at: string;
}

export interface PortfolioItem {
  id: number;
  title: string;
  description: string;
  url: string;
  tags: string[];
  created_at: string;
}

export interface RateCardEntry {
  id: number;
  skill_category: string;
  hourly_rate: number | null;
  fixed_min: number | null;
  currency: string;
}

// ---- Contract Addendum v2 — Orchestration Engine ----

export interface SearchProfile {
  id: number;
  name: string;
  keyword_group_id?: number | null;
  filter_id?: number | null;
  boolean_query: string; // "(React OR Next.js) AND (NOT WordPress)"
  auto_queue_proposals: boolean;
  created_at: string;
}

export type AccountMode = 'api' | 'stealth' | 'hybrid' | 'disabled';
export const ACCOUNT_MODES: AccountMode[] = ['api', 'stealth', 'hybrid', 'disabled'];

export interface PlatformAccount {
  id: number;
  platform: Platform;
  label: string;
  principal: string;
  mode: AccountMode;
  enabled: boolean;
  credential_ref: string; // pointer into the credential vault — never a secret itself
  // Per-platform extras. Recognized keys:
  //   bidder_id (number)     — Freelancer: required before proposals can be submitted
  //   on_behalf_of (string)  — Upwork: agency member the browser worker acts as
  settings: Record<string, unknown>;
  created_at: string;
}

export type ProposalStatus =
  | 'pending_review'
  | 'approved'
  | 'rejected'
  | 'queued_for_browser' // Upwork: waiting on the external browser worker; flips to 'submitted' when done
  | 'submitted'
  | 'failed'
  | 'generation_failed';
export const PROPOSAL_STATUSES: ProposalStatus[] = [
  'pending_review',
  'approved',
  'rejected',
  'queued_for_browser',
  'submitted',
  'failed',
  'generation_failed',
];

export interface ProposalQueueItem {
  id: number;
  job_id: number;
  platform: Platform;
  proposal_text: string;
  bid_amount: number | null;
  bid_period_days: number | null;
  portfolio_item_ids: number[];
  template_id: number | null;
  status: ProposalStatus;
  reviewed_by: string | null;
  reviewed_at: string | null;
  submission_result: Record<string, unknown>;
  created_at: string;
  job?: Job; // embedded on responses
  // ---- v3 fields ----
  humanized_text: string | null; // anti-detection pass output; editor prefers this over proposal_text
  bid_rationale: string | null; // e.g. "160h est. × $75/hr × 1.35 complexity = $16,200"
  portfolio_match: Record<number, PortfolioMatchEntry>; // auto-selected portfolio, itemId → match info
  analysis: ProposalAnalysis | null;
  confidence: number; // 0–100
  needs_review: boolean; // true when confidence < 50
  versions: ProposalVersion[];
  rejection_reason: string | null;
  outcome: ProposalOutcome;
  request_type: ProposalRequestType;
  bid_advice: BidAdvice | null; // Phase 3: bid/caution/skip recommendation
  client_replied_at: string | null; // set when the client answers on the platform (see client_replied WS event)
}

export interface ProposalReviewAction {
  reviewer: string;
  proposal_text?: string;
  bid_amount?: number;
  bid_period_days?: number;
  template_id?: number; // reviewer started from this template — reuse it instead of minting
  save_as_template?: boolean; // default true server-side; false opts out of minting a template
}

// GET /api/proposals — always paginated
export interface ProposalsPage {
  items: ProposalQueueItem[];
  total: number;
}

// v3: reject requires a reason — feeds rejection learning
export interface ProposalRejectAction {
  reviewer: string;
  reason: RejectionReason;
  notes?: string;
}

// ---- Contract Addendum v3 — Proposal Generation, Anti-Detection, Gig/Seller Mode ----

export type ProposalOutcome = 'pending' | 'hired' | 'rejected' | 'ghosted';
export const PROPOSAL_OUTCOMES: ProposalOutcome[] = ['pending', 'hired', 'rejected', 'ghosted'];

export type RejectionReason =
  | 'too_generic'
  | 'too_expensive'
  | 'wrong_tone'
  | 'overpromising'
  | 'other';
export const REJECTION_REASONS: RejectionReason[] = [
  'too_generic',
  'too_expensive',
  'wrong_tone',
  'overpromising',
  'other',
];

export type ProposalRequestType = 'job' | 'buyer_request' | 'follow_up';

// Phase 3: should we bid on this at all — derived from win-rate learning
export interface BidAdvice {
  recommendation: 'bid' | 'caution' | 'skip';
  reason: string;
}

// Phase 3: GET /api/proposals/{id}/interview-prep
export interface InterviewPrepQuestion {
  question: string;
  suggested_answer: string;
}

export interface InterviewPrep {
  questions: InterviewPrepQuestion[];
  pain_points: string[];
  red_flags: string[];
  talking_points: string[];
}

export interface ProposalVersion {
  text: string;
  bid: number | null;
  by: string;
  at: string;
}

export interface PortfolioMatchEntry {
  title: string;
  overlap_pct: number;
  matched_skills: string[];
}

export interface ProposalAnalysis {
  required_skills: string[];
  deliverables: string[];
  client_pain_points: string[];
  tone: string;
  missing_info: string[];
  red_flags: string[];
  strengths: string[];
  gaps: string[];
}

// Template (proposal learning loop — saved on approve, win rate from outcomes)
export interface Template {
  id: number;
  title: string;
  platform: Platform;
  text: string;
  bid: number | null;
  tags: string[];
  uses: number;
  wins: number;
  losses: number;
  win_rate: number;
  created_at: string;
}

// ---- Phase 2: win-rate analytics (`GET /api/analytics/funnel`) ----
// win_rate is a 0–100 float, or null when no outcomes have been recorded yet.

export interface FunnelCounts {
  queued: number;
  approved: number;
  submitted: number;
  replied: number;
  hired: number;
  rejected: number;
  ghosted: number;
}

export interface PlatformFunnelRow {
  platform: Platform;
  queued: number;
  approved: number;
  submitted: number;
  replied: number;
  hired: number;
  win_rate: number | null;
}

export interface TemplateFunnelRow {
  template_id: number;
  title: string;
  platform: Platform;
  uses: number;
  wins: number;
  losses: number;
  win_rate: number | null;
}

export interface BidBandRow {
  band: string;
  submitted: number;
  hired: number;
  win_rate: number | null;
}

export interface RejectionReasonCount {
  reason: string;
  count: number;
}

export interface FunnelAnalytics {
  funnel: FunnelCounts;
  by_platform: PlatformFunnelRow[];
  by_template: TemplateFunnelRow[];
  by_bid_band: BidBandRow[];
  rejection_reasons: RejectionReasonCount[];
}

// GET /api/analytics/trend?weeks=N — weekly rollup, win_rate 0–100 or null
export interface TrendWeek {
  week: string;
  submitted: number;
  replied: number;
  hired: number;
  win_rate: number | null;
}

export interface TrendAnalytics {
  weeks: TrendWeek[];
}

// Contract Addendum v6 — credential enrollment status (key names only, never values)
export interface CredentialStatus {
  enrolled: boolean;
  keys: string[];
  updated_at: string | null;
}

export type GigStatus = 'draft' | 'active' | 'paused';
export const GIG_STATUSES: GigStatus[] = ['draft', 'active', 'paused'];

export interface Gig {
  id: number;
  platform: Platform;
  title: string;
  external_id: string | null;
  url: string;
  status: GigStatus;
  price_min: number | null;
  template_id: number | null;
}

export interface GigMetricSuggestion {
  area: string;
  message: string;
}

export interface GigMetric {
  week: string;
  impressions: number;
  clicks: number;
  orders: number;
  revenue: number;
  suggestions: GigMetricSuggestion[];
}

// Fiverr gig template — structured JSON editor
export interface GigPricingTier {
  price: number | null;
  delivery_days: number | null;
  revisions: number | null;
}

export interface GigTemplateJson {
  title: string;
  category: string;
  subcategory: string;
  tags: string[];
  pricing: {
    basic: GigPricingTier;
    standard: GigPricingTier;
    premium: GigPricingTier;
  };
  description: {
    hook: string;
    what_you_get: string;
    why_me: string;
    cta: string;
  };
  faqs: { question: string; answer: string }[];
}

export interface GigTemplate {
  id: number;
  platform: Platform;
  name: string;
  template_json: Partial<GigTemplateJson>;
  auto_publish: boolean;
  is_active: boolean;
  created_at?: string;
}

export interface CompetitorGig {
  title: string;
  price: number | null;
  [key: string]: unknown;
}

export interface CompetitorSnapshot {
  id: number;
  platform: Platform;
  category: string;
  gigs: CompetitorGig[];
  insights: string[];
  created_at: string;
}

// Scoring weights v2 (server-side, from contract — for UI display)
export const SCORING_WEIGHTS: { component: string; points: number; description: string }[] = [
  { component: 'keyword_match', points: 25, description: 'Primary exact keyword matches (15) plus secondary fuzzy matches (10) against the active keyword group.' },
  { component: 'budget_realism', points: 25, description: 'Budget vs. estimated hours × market rate from the rate card ($50/h fallback).' },
  { component: 'client_verification', points: 20, description: 'Client trust signals: payment verified (+9), identity verified (+5), past hires (up to +6).' },
  { component: 'description_quality', points: 20, description: 'Detail and specificity: >100 words (+6), clear deliverables (+8), tech requirements (+6); vague postings score −20.' },
  { component: 'urgency_ratio', points: 10, description: 'Budget ÷ complexity ÷ timeline days — higher ratio, better pay for the pressure.' },
  { component: 'red_flag_penalty', points: -60, description: '−30 per red flag (capped at −60): "unlimited revisions", "test task before hire", "work for exposure/review", "no upfront/milestone", "urgent + low budget", "student/budget project", equity-only.' },
];
