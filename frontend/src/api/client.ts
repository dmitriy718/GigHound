import type {
  AlertSettings,
  CompetitorSnapshot,
  Gig,
  GigMetric,
  GigStatus,
  GigTemplate,
  GigTemplateJson,
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
  ProposalStatus,
  RateCardEntry,
  SearchFilter,
  SearchProfile,
  Template,
} from '../types';

export const API_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined) ?? 'http://localhost:8000';

export function wsUrl(path: string): string {
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(API_URL + path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });
  } catch (e) {
    throw new ApiError(0, `Cannot reach backend at ${API_URL}`);
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
export const ingestJobs = (jobs: JobIngest[]) =>
  request<{ ingested: number; auto_archived: number; alerts_sent: number }>('/api/jobs/ingest', {
    method: 'POST',
    body: JSON.stringify({ jobs }),
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

export const getProposals = (status?: ProposalStatus) =>
  request<ProposalQueueItem[]>(`/api/proposals${status ? `?status=${encodeURIComponent(status)}` : ''}`);
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

// ---- Proposals v3 (learning loop) ----

export const bulkApproveProposals = (ids: number[], reviewer: string) =>
  request<{ approved: number; skipped: number }>('/api/proposals/bulk-approve', {
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
  request<{ enqueued?: number }>('/api/gigs/metrics/scrape', { method: 'POST' });

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

// ---- Platform accounts ----

export type PlatformAccountPayload = Omit<PlatformAccount, 'id' | 'created_at'>;

export const getAccounts = () => request<PlatformAccount[]>('/api/accounts');
export const createAccount = (body: PlatformAccountPayload) =>
  request<PlatformAccount>('/api/accounts', { method: 'POST', body: JSON.stringify(body) });
export const updateAccount = (id: number, body: PlatformAccountPayload) =>
  request<PlatformAccount>(`/api/accounts/${id}`, { method: 'PUT', body: JSON.stringify(body) });
export const deleteAccount = (id: number) =>
  request<void>(`/api/accounts/${id}`, { method: 'DELETE' });
