import type {
  AlertSettings,
  User,
  CompetitorSnapshot,
  CredentialStatus,
  FunnelAnalytics,
  Gig,
  GigMetric,
  GigStatus,
  GigTemplate,
  GigTemplateJson,
  InterviewPrep,
  Job,
  JobIngest,
  Keyword,
  KeywordGroup,
  Platform,
  PlatformAccount,
  PortfolioItem,
  ProfileTemplate,
  ProposalOutcome,
  ProposalQueueItem,
  ProposalRejectAction,
  ProposalReviewAction,
  ProposalsPage,
  ProposalStatus,
  RateCardEntry,
  ScoreBreakdown,
  SearchFilter,
  SearchProfile,
  Template,
  TrendAnalytics,
} from '../types';

// Empty string = same origin (the backend serves SPA + API + WS together);
// VITE_API_URL only overrides this for split dev setups.
export const API_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined) ?? '';

export function wsUrl(path: string): string {
  if (!API_URL) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}${path}`;
  }
  return API_URL.replace(/^http/, 'ws') + path;
}

export class ApiError extends Error {
  status: number;
  detail: unknown; // parsed response body when it is JSON (e.g. 422 {detail: {validation: [...]}})
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

// ---- Auth token (AD-2: localStorage, 12h JWT) ----

const TOKEN_KEY = 'gighound_token';

export const getToken = (): string | null => localStorage.getItem(TOKEN_KEY);
export const setToken = (token: string) => localStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

// App registers a handler that drops the session and shows Login on any 401.
let onUnauthorized: (() => void) | null = null;
export const setOnUnauthorized = (fn: (() => void) | null) => {
  onUnauthorized = fn;
};

// Same effect as a 401 in request(): clear the token and drop the session.
// Used by the WS hook when the server closes the socket with 4401.
export const forceUnauthorized = () => {
  clearToken();
  onUnauthorized?.();
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  const token = getToken();
  try {
    res = await fetch(API_URL + path, {
      // a hung connection must not spin a loading state forever;
      // a caller-supplied signal (spread below) wins over the timeout
      signal: AbortSignal.timeout(30_000),
      ...init,
      // merged AFTER ...init so a caller-supplied header never drops
      // Authorization; Content-Type only on requests with a body (GETs
      // without it skip the CORS preflight)
      headers: {
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...((init?.headers ?? {}) as Record<string, string>),
      },
    });
  } catch (e) {
    throw new ApiError(0, `Cannot reach backend at ${API_URL}`);
  }
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    clearToken();
    onUnauthorized?.();
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    let detail: unknown;
    try {
      detail = text ? JSON.parse(text) : undefined;
    } catch {
      detail = undefined;
    }
    throw new ApiError(res.status, `${res.status} ${res.statusText}${text ? `: ${text}` : ''}`, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- Auth ----

export interface AuthResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export const login = (body: { email: string; password: string }) =>
  request<AuthResponse>('/api/auth/login', { method: 'POST', body: JSON.stringify(body) });
export const register = (body: { email: string; password: string; display_name: string }) =>
  request<AuthResponse>('/api/auth/register', { method: 'POST', body: JSON.stringify(body) });
export const getMe = () => request<User>('/api/auth/me');
export const logout = () =>
  request<{ status: string }>('/api/auth/logout', { method: 'POST' });
export const changePassword = (body: { current_password: string; new_password: string }) =>
  request<{ status: string }>('/api/auth/password', { method: 'POST', body: JSON.stringify(body) });
export const deleteMyAccount = (body: { password: string }) =>
  request<{ status: string }>('/api/auth/account', { method: 'DELETE', body: JSON.stringify(body) });

// ---- Keyword intelligence ----

export interface KeywordGroupPayload {
  name: string;
  service_type: string;
  keywords: Keyword[];
}

export const getKeywordGroups = () => request<KeywordGroup[]>('/api/keyword-groups');
export const createKeywordGroup = (body: KeywordGroupPayload) =>
  request<KeywordGroup>('/api/keyword-groups', { method: 'POST', body: JSON.stringify(body) });
export const updateKeywordGroup = (id: number, body: KeywordGroupPayload) =>
  request<KeywordGroup>(`/api/keyword-groups/${id}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteKeywordGroup = (id: number) =>
  request<void>(`/api/keyword-groups/${id}`, { method: 'DELETE' });
export const suggestSkills = (platform: Platform, q: string) =>
  request<{ suggestions: string[] }>(
    `/api/skills/suggest?platform=${encodeURIComponent(platform)}&q=${encodeURIComponent(q)}`,
  );

// ---- Search fine-tuning ----

export type SearchFilterPayload = Omit<SearchFilter, 'id' | 'created_at'>;

export const getFilters = () => request<SearchFilter[]>('/api/filters');
export const createFilter = (body: SearchFilterPayload) =>
  request<SearchFilter>('/api/filters', { method: 'POST', body: JSON.stringify(body) });
export const updateFilter = (id: number, body: SearchFilterPayload) =>
  request<SearchFilter>(`/api/filters/${id}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteFilter = (id: number) =>
  request<void>(`/api/filters/${id}`, { method: 'DELETE' });
export const previewFilter = (id: number) =>
  request<{ matched: Job[]; excluded_count: number }>(`/api/filters/${id}/preview`, { method: 'POST' });

// ---- Jobs feed & scoring ----

export interface JobsQuery {
  status?: string;
  platform?: Platform;
  min_score?: number;
  limit?: number;
  offset?: number;
}

export const getJobs = (query: JobsQuery = {}) => {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== '') params.set(k, String(v));
  }
  const qs = params.toString();
  return request<{ jobs: Job[]; total: number }>(`/api/jobs${qs ? `?${qs}` : ''}`);
};
export const getJob = (id: number) => request<Job>(`/api/jobs/${id}`);
export const archiveJob = (id: number) =>
  request<Job>(`/api/jobs/${id}/archive`, { method: 'POST' });
export const unarchiveJob = (id: number) =>
  request<Job>(`/api/jobs/${id}/unarchive`, { method: 'POST' });
export const bulkArchiveJobs = (ids: number[]) =>
  request<{ archived: number[]; skipped: number[] }>('/api/jobs/bulk-archive', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  });
export const ingestJobs = (jobs: JobIngest[]) =>
  request<{ ingested: number; auto_archived: number; alerts_sent: number }>('/api/jobs/ingest', {
    method: 'POST',
    body: JSON.stringify({ jobs }),
  });

// Dry-run scoring — scores a job payload without persisting anything or triggering proposals
export interface ScorePreviewResult {
  quality_score: number;
  score_breakdown: ScoreBreakdown;
  red_flags: string[];
}
export const scorePreview = (job: JobIngest) =>
  request<ScorePreviewResult>('/api/jobs/score-preview', {
    method: 'POST',
    body: JSON.stringify({ job }),
  });

// ---- Alerts ----

export const getAlertSettings = () => request<AlertSettings>('/api/alerts/settings');
export const updateAlertSettings = (body: AlertSettings) =>
  request<AlertSettings>('/api/alerts/settings', { method: 'PUT', body: JSON.stringify(body) });
export const getDigestPreview = () => request<{ jobs: Job[] }>('/api/alerts/digest-preview');
export const sendDigest = () =>
  request<{ jobs_in_digest: number; emailed: boolean }>('/api/alerts/digest/send', {
    method: 'POST',
  });
// One-time WS auth ticket — keeps the JWT out of the WS query string (access logs).
export const getWsTicket = () =>
  request<{ ticket: string }>('/api/alerts/ws-ticket', { method: 'POST' });

// ---- Profile management ----

export interface ProfileTemplatePayload {
  platform: Platform;
  name: string;
  pitch_template: string;
}

export const getProfileTemplates = (platform?: Platform) =>
  request<ProfileTemplate[]>(
    `/api/profiles/templates${platform ? `?platform=${encodeURIComponent(platform)}` : ''}`,
  );
export const createProfileTemplate = (body: ProfileTemplatePayload) =>
  request<ProfileTemplate>('/api/profiles/templates', { method: 'POST', body: JSON.stringify(body) });
export const updateProfileTemplate = (id: number, body: ProfileTemplatePayload) =>
  request<ProfileTemplate>(`/api/profiles/templates/${id}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteProfileTemplate = (id: number) =>
  request<void>(`/api/profiles/templates/${id}`, { method: 'DELETE' });

export interface PortfolioItemPayload {
  title: string;
  description: string;
  url: string;
  tags: string[];
}

export const getPortfolioItems = () => request<PortfolioItem[]>('/api/profiles/portfolio');
export const createPortfolioItem = (body: PortfolioItemPayload) =>
  request<PortfolioItem>('/api/profiles/portfolio', { method: 'POST', body: JSON.stringify(body) });
export const updatePortfolioItem = (id: number, body: PortfolioItemPayload) =>
  request<PortfolioItem>(`/api/profiles/portfolio/${id}`, { method: 'PUT', body: JSON.stringify(body) });
export const deletePortfolioItem = (id: number) =>
  request<void>(`/api/profiles/portfolio/${id}`, { method: 'DELETE' });

export interface RateCardEntryPayload {
  skill_category: string;
  hourly_rate: number | null;
  fixed_min: number | null;
  currency: string;
}

export const getRateCard = () => request<RateCardEntry[]>('/api/profiles/rate-card');
export const createRateCardEntry = (body: RateCardEntryPayload) =>
  request<RateCardEntry>('/api/profiles/rate-card', { method: 'POST', body: JSON.stringify(body) });
export const updateRateCardEntry = (id: number, body: RateCardEntryPayload) =>
  request<RateCardEntry>(`/api/profiles/rate-card/${id}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteRateCardEntry = (id: number) =>
  request<void>(`/api/profiles/rate-card/${id}`, { method: 'DELETE' });

// ---- Proposal review queue (human-in-the-loop boundary) ----

export interface ProposalsQuery {
  status?: ProposalStatus;
  request_type?: string;
  limit?: number;
  offset?: number;
}

export const getProposals = (query: ProposalsQuery = {}) => {
  const params = new URLSearchParams();
  if (query.status) params.set('status', query.status);
  if (query.request_type) params.set('request_type', query.request_type);
  if (query.limit !== undefined) params.set('limit', String(query.limit));
  if (query.offset !== undefined) params.set('offset', String(query.offset));
  const qs = params.toString();
  return request<ProposalsPage>(`/api/proposals${qs ? `?${qs}` : ''}`);
};
export const getProposal = (id: number) => request<ProposalQueueItem>(`/api/proposals/${id}`);
export const approveProposal = (id: number, body: ProposalReviewAction) =>
  request<ProposalQueueItem>(`/api/proposals/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
export const rejectProposal = (id: number, body: ProposalRejectAction) =>
  request<ProposalQueueItem>(`/api/proposals/${id}/reject`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
export const submitProposal = (id: number) =>
  request<ProposalQueueItem>(`/api/proposals/${id}/submit`, { method: 'POST' });
// Record a BY-HAND submission on platforms with no automated channel
export const markProposalSubmitted = (id: number, channel?: string) =>
  request<ProposalQueueItem>(`/api/proposals/${id}/mark-submitted`, {
    method: 'POST',
    body: JSON.stringify(channel ? { channel } : {}),
  });

// ---- Proposals v3 (learning loop) ----

export const bulkApproveProposals = (ids: number[], reviewer: string) =>
  request<{ approved: number[]; skipped: number[] }>('/api/proposals/bulk-approve', {
    method: 'POST',
    body: JSON.stringify({ ids, reviewer }),
  });
export const markProposalOutcome = (id: number, outcome: Exclude<ProposalOutcome, 'pending'>) =>
  request<ProposalQueueItem>(`/api/proposals/${id}/outcome`, {
    method: 'POST',
    body: JSON.stringify({ outcome }),
  });
export const revertProposal = (id: number, versionIndex: number) =>
  request<ProposalQueueItem>(`/api/proposals/${id}/revert`, {
    method: 'POST',
    body: JSON.stringify({ version_index: versionIndex }),
  });
export const suggestProposalTemplates = (platform: Platform, skills: string[]) => {
  const params = new URLSearchParams({ platform, skills: skills.join(',') });
  return request<Template[]>(`/api/proposals/templates/suggest?${params.toString()}`);
};

// ---- Phase 3: winning-advantage features ----

// Draft a follow-up for an already-submitted proposal (409 when not eligible)
export const followUpProposal = (id: number) =>
  request<ProposalQueueItem>(`/api/proposals/${id}/follow-up`, { method: 'POST' });
export const getInterviewPrep = (id: number) =>
  request<InterviewPrep>(`/api/proposals/${id}/interview-prep`);
// Re-run generation for a generation_failed item (resets its auto-retry budget)
export const retryProposalGeneration = (id: number) =>
  request<ProposalQueueItem>(`/api/proposals/${id}/retry-generation`, { method: 'POST' });

// ---- Gigs (`/api/gigs`) ----

export interface GigPayload {
  platform: Platform;
  title: string;
  external_id: string | null;
  url: string;
  status: GigStatus;
  price_min: number | null;
  template_id: number | null;
}

export const getGigs = (platform?: Platform) =>
  request<Gig[]>(`/api/gigs${platform ? `?platform=${encodeURIComponent(platform)}` : ''}`);
export const registerGig = (body: GigPayload) =>
  request<Gig>('/api/gigs', { method: 'POST', body: JSON.stringify(body) });
export const getGigMetrics = (gigId: number) =>
  request<GigMetric[]>(`/api/gigs/metrics?gig_id=${encodeURIComponent(gigId)}`);
export const triggerGigScrape = () =>
  request<{ queued_tasks: number[] }>('/api/gigs/metrics/scrape', { method: 'POST' });

export interface GigTemplatePayload {
  platform: Platform;
  name: string;
  template_json: Partial<GigTemplateJson>;
  auto_publish: boolean;
}

export const getGigTemplates = (platform?: Platform) =>
  request<GigTemplate[]>(
    `/api/gigs/templates${platform ? `?platform=${encodeURIComponent(platform)}` : ''}`,
  );
export const createGigTemplate = (body: GigTemplatePayload) =>
  request<GigTemplate>('/api/gigs/templates', { method: 'POST', body: JSON.stringify(body) });
export const updateGigTemplate = (id: number, body: GigTemplatePayload) =>
  request<GigTemplate>(`/api/gigs/templates/${id}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteGigTemplate = (id: number) =>
  request<void>(`/api/gigs/templates/${id}`, { method: 'DELETE' });
export const toggleGigTemplate = (id: number) =>
  request<GigTemplate>(`/api/gigs/templates/${id}/toggle`, { method: 'POST' });
export const createGigFromTemplate = (id: number) =>
  request<{ stealth_task_id: number; status: string; note: string }>(
    `/api/gigs/templates/${id}/create-gig`,
    { method: 'POST' },
  );

export const getFiverrTaxonomy = () =>
  request<{ categories: Record<string, string[]> }>('/api/gigs/taxonomy/fiverr');
export const seoTitleScore = (title: string, keywords: string[]) =>
  request<{ score: number; issues: string[] }>('/api/gigs/seo-title-score', {
    method: 'POST',
    body: JSON.stringify({ title, keywords }),
  });
export const generateFaqs = (gigType: string, title: string, count: number) =>
  request<{ faqs: { question: string; answer: string }[] }>('/api/gigs/faqs/generate', {
    method: 'POST',
    body: JSON.stringify({ gig_type: gigType, title, count }),
  });
export const getCompetitors = (platform: Platform, category: string) =>
  request<CompetitorSnapshot[]>(
    `/api/gigs/competitors?platform=${encodeURIComponent(platform)}&category=${encodeURIComponent(category)}`,
  );
export const getBuyerRequests = () =>
  request<{ offers_remaining_today: number; daily_limit: number; count: number }>(
    '/api/gigs/buyer-requests',
  );

// ---- Search profiles (incl. boolean builder) ----

export type SearchProfilePayload = Omit<SearchProfile, 'id' | 'created_at'>;

export const getSearchProfiles = () => request<SearchProfile[]>('/api/search-profiles');
export const createSearchProfile = (body: SearchProfilePayload) =>
  request<SearchProfile>('/api/search-profiles', { method: 'POST', body: JSON.stringify(body) });
export const updateSearchProfile = (id: number, body: SearchProfilePayload) =>
  request<SearchProfile>(`/api/search-profiles/${id}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
export const deleteSearchProfile = (id: number) =>
  request<void>(`/api/search-profiles/${id}`, { method: 'DELETE' });
export const validateBooleanQuery = (query: string) =>
  request<{ valid: boolean; error?: string; ast?: string }>(
    '/api/search-profiles/validate-boolean',
    { method: 'POST', body: JSON.stringify({ query }) },
  );
export const runSearchProfileNow = (id: number) =>
  request<{ queued: boolean; platforms: string[] }>(`/api/search-profiles/${id}/run-now`, {
    method: 'POST',
  });

// ---- Analytics (learning loop) ----

export const getFunnelAnalytics = () => request<FunnelAnalytics>('/api/analytics/funnel');
export const getAnalyticsTrend = (weeks = 8) =>
  request<TrendAnalytics>(`/api/analytics/trend?weeks=${encodeURIComponent(weeks)}`);

// ---- Platform accounts ----

export type PlatformAccountPayload = Omit<PlatformAccount, 'id' | 'created_at'>;

export const getAccounts = () => request<PlatformAccount[]>('/api/accounts');
export const createAccount = (body: PlatformAccountPayload) =>
  request<PlatformAccount>('/api/accounts', { method: 'POST', body: JSON.stringify(body) });
export const updateAccount = (id: number, body: PlatformAccountPayload) =>
  request<PlatformAccount>(`/api/accounts/${id}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteAccount = (id: number) =>
  request<void>(`/api/accounts/${id}`, { method: 'DELETE' });

// ---- Credential enrollment (Contract Addendum v6 — secrets go to the vault, never returned) ----

export const enrollCredentials = (accountId: number, secrets: Record<string, string>) =>
  request<void>(`/api/accounts/${accountId}/credentials`, {
    method: 'POST',
    body: JSON.stringify({ secrets }),
  });
export const getCredentialStatus = (accountId: number) =>
  request<CredentialStatus>(`/api/accounts/${accountId}/credentials/status`);
export const deleteCredentials = (accountId: number) =>
  request<void>(`/api/accounts/${accountId}/credentials`, { method: 'DELETE' });
export const startFreelancerOAuth = (accountId: number) =>
  request<{ authorize_url: string }>(`/api/accounts/${accountId}/oauth/freelancer/start`);
export const completeFreelancerOAuth = (accountId: number, code: string) =>
  request<void>(`/api/accounts/${accountId}/oauth/freelancer/complete`, {
    method: 'POST',
    body: JSON.stringify({ code }),
  });
