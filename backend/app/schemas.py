from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

Platform = Literal["upwork", "fiverr", "freelancer", "peopleperhour", "guru", "linkedin", "indeed"]
KeywordKind = Literal["primary", "secondary", "negative"]
JobType = Literal["fixed", "hourly", "retainer", "contest", "gig"]
ExperienceLevel = Literal["entry", "intermediate", "expert"]
WorkArrangement = Literal["remote", "onsite", "hybrid"]


# ---------- Auth ----------

class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=72)  # 72 = bcrypt input limit
    display_name: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)  # 72 = bcrypt input limit


class AccountDeleteIn(BaseModel):
    password: str


# ---------- Keywords ----------

class KeywordIn(BaseModel):
    term: str
    kind: KeywordKind
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class KeywordOut(KeywordIn):
    id: int

    class Config:
        from_attributes = True


class KeywordGroupIn(BaseModel):
    name: str
    service_type: str = ""
    keywords: list[KeywordIn] = []


class KeywordGroupOut(BaseModel):
    id: int
    name: str
    service_type: str
    created_at: datetime
    keywords: list[KeywordOut] = []

    class Config:
        from_attributes = True


# ---------- Search filters ----------

class ClientFilters(BaseModel):
    payment_verified: Optional[bool] = None
    min_hire_rate: Optional[float] = None
    min_total_spent: Optional[float] = None
    countries: list[str] = []


class PlatformBudget(BaseModel):
    platform: Platform
    min: Optional[float] = None
    max: Optional[float] = None
    currency: str = "USD"


class SearchFilterIn(BaseModel):
    name: str
    keyword_group_id: Optional[int] = None
    platforms: list[Platform] = []
    job_types: list[JobType] = []
    budgets: list[PlatformBudget] = []
    experience_levels: list[ExperienceLevel] = []
    client_filters: ClientFilters = ClientFilters()
    posted_within_hours: Optional[int] = None
    apply_deadline_within_hours: Optional[int] = None
    work_arrangements: list[WorkArrangement] = []
    languages: list[str] = []
    max_proposals: Optional[int] = None
    quality_threshold: float = Field(default=40.0, ge=0, le=100)


class SearchFilterOut(SearchFilterIn):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Jobs ----------

class ClientInfo(BaseModel):
    payment_verified: Optional[bool] = None
    identity_verified: Optional[bool] = None
    hire_rate: Optional[float] = None
    past_hires: Optional[int] = None
    total_spent: Optional[float] = None
    country: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    client_id: Optional[str] = None  # platform-side client identifier, when the API exposes one
    name: Optional[str] = None       # client display name/username, when exposed


class JobIngest(BaseModel):
    external_id: str
    platform: Platform
    title: str
    description: str = ""
    url: str = ""
    job_type: Optional[JobType] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    currency: str = "USD"
    experience_level: Optional[ExperienceLevel] = None
    client_info: ClientInfo = ClientInfo()
    proposals_count: Optional[int] = None
    skills: list[str] = []
    languages: list[str] = []
    work_arrangement: Optional[WorkArrangement] = None
    posted_at: Optional[datetime] = None
    apply_deadline: Optional[datetime] = None

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        """Job content is untrusted input — no javascript:/file:/data: URLs."""
        if v and urlparse(v).scheme.lower() not in ("http", "https"):
            raise ValueError("url must use the http or https scheme")
        return v


class IngestJobsIn(BaseModel):
    jobs: list[JobIngest] = []


class ClientHistoryOut(BaseModel):
    past_proposals: int
    hired: int
    rejected: int
    ghosted: int


class JobOut(BaseModel):
    id: int
    external_id: str
    platform: str
    title: str
    description: str
    url: str
    job_type: Optional[str]
    budget_min: Optional[float]
    budget_max: Optional[float]
    currency: str
    budget_usd_min: Optional[float]
    budget_usd_max: Optional[float]
    experience_level: Optional[str]
    client_info: dict
    proposals_count: Optional[int]
    skills: list[str]
    languages: list[str]
    work_arrangement: Optional[str]
    posted_at: Optional[datetime]
    apply_deadline: Optional[datetime]
    quality_score: float
    score_breakdown: dict
    red_flags: list[str]
    status: str
    is_duplicate: bool
    duplicate_of: Optional[int]
    fetched_at: datetime
    # populated only by GET /api/jobs/{id}; null when this client was never seen
    client_history: Optional[ClientHistoryOut] = None

    class Config:
        from_attributes = True


class IngestResult(BaseModel):
    ingested: int
    auto_archived: int
    alerts_sent: int


class BulkArchiveAction(BaseModel):
    ids: list[int]


class ScorePreviewIn(BaseModel):
    job: JobIngest


class ScorePreviewOut(BaseModel):
    quality_score: float
    score_breakdown: dict
    red_flags: list[str]


class PreviewResult(BaseModel):
    matched: list[JobOut]
    excluded_count: int


# ---------- Alerts ----------

class AlertSettingsSchema(BaseModel):
    realtime_enabled: bool = True
    min_score_alert: float = Field(default=70.0, ge=0, le=100)
    digest_mode: Literal["off", "hourly", "daily"] = "off"
    hot_job_enabled: bool = True
    hot_job_max_proposals: int = 5        # hot = <5 proposals
    hot_job_posted_hours: int = 1         # posted <1 hour ago
    hot_job_min_score: float = Field(default=90.0, ge=0, le=100)  # and score >90

    class Config:
        from_attributes = True


# ---------- Orchestration: search profiles, accounts, proposal queue ----------

class SearchProfileIn(BaseModel):
    name: str
    keyword_group_id: Optional[int] = None
    filter_id: Optional[int] = None
    boolean_query: str = ""               # e.g. "(React OR Next.js) AND (NOT WordPress)"
    auto_queue_proposals: bool = True


class SearchProfileOut(SearchProfileIn):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class PlatformAccountIn(BaseModel):
    platform: Platform
    label: str
    principal: str = "default"
    mode: Literal["api", "stealth", "hybrid", "disabled"] = "api"
    enabled: bool = True
    credential_ref: str = ""
    # recognized keys: bidder_id (freelancer user id), on_behalf_of (upwork agency member)
    settings: dict = {}


class PlatformAccountOut(PlatformAccountIn):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class CredentialsIn(BaseModel):
    """Secret key/value pairs to store in the vault for a platform account.

    Recognized keys per platform (others are rejected 422):
      freelancer/upwork: access_token (+ optional refresh_token)
      stealth platforms: storage_state_json (Playwright storage_state string)
                         OR username + password (fallback-only login)
    """
    secrets: dict[str, str]


class CredentialStatusOut(BaseModel):
    enrolled: bool
    keys: list[str]
    updated_at: Optional[datetime] = None


class OAuthCompleteIn(BaseModel):
    code: str
    redirect_uri: Optional[str] = None  # defaults to FREELANCER_REDIRECT_URI


class BidAdviceOut(BaseModel):
    recommendation: Literal["bid", "caution", "skip"]
    reason: str


class ProposalQueueOut(BaseModel):
    id: int
    job_id: int
    platform: str
    proposal_text: str
    humanized_text: str = ""
    bid_amount: Optional[float]
    bid_period_days: Optional[int]
    bid_rationale: str = ""
    bid_advice: Optional[BidAdviceOut] = None
    portfolio_item_ids: list[int]
    portfolio_match: dict = {}
    template_id: Optional[int]
    analysis: dict = {}
    confidence: float = 0.0
    needs_review: bool = False
    versions: list[dict] = []
    status: str
    rejection_reason: Optional[str] = None
    outcome: str = "pending"
    request_type: str = "job"
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    client_replied_at: Optional[datetime] = None
    submission_result: dict
    created_at: datetime
    job: Optional[JobOut] = None

    class Config:
        from_attributes = True


class ProposalReviewAction(BaseModel):
    reviewer: str
    proposal_text: Optional[str] = None   # reviewer may edit before approving
    bid_amount: Optional[float] = None
    bid_period_days: Optional[int] = None
    # reviewer picked an existing template from suggestions: reuse it on
    # approve instead of minting a new one
    template_id: Optional[int] = None
    # set False to skip minting a Template from this approval
    save_as_template: bool = True


class ProposalRejectAction(BaseModel):
    reviewer: str
    reason: Literal["too_generic", "too_expensive", "wrong_tone", "overpromising", "other"] = "other"
    notes: str = ""


class BulkApproveAction(BaseModel):
    ids: list[int]
    reviewer: str


class OutcomeAction(BaseModel):
    outcome: Literal["hired", "rejected", "ghosted"]


class InterviewQuestion(BaseModel):
    question: str
    suggested_answer: str


class InterviewPrepOut(BaseModel):
    questions: list[InterviewQuestion]
    pain_points: list[str] = []
    red_flags: list[str] = []
    talking_points: list[str] = []


# ---------- Proposal generation v3 / templates / gigs ----------

class TemplateOut(BaseModel):
    id: int
    title: str
    platform: str
    text: str
    bid: Optional[float]
    tags: list[str]
    uses: int
    wins: int
    losses: int
    win_rate: float
    created_at: datetime

    class Config:
        from_attributes = True


class GigTemplateIn(BaseModel):
    platform: Platform
    name: str
    template_json: dict = {}
    auto_publish: bool = False


class GigTemplateOut(GigTemplateIn):
    id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class GigOut(BaseModel):
    id: int
    platform: str
    template_id: Optional[int]
    external_id: str
    title: str
    status: str
    url: str
    price_min: Optional[float]
    created_at: datetime

    class Config:
        from_attributes = True


class StealthTaskClaimIn(BaseModel):
    worker_id: str


class GigMetricIn(BaseModel):
    gig_id: int
    impressions: int = 0
    clicks: int = 0
    orders: int = 0
    revenue: float = 0.0
    week: Optional[str] = None


class GigMetricOut(GigMetricIn):
    id: int
    suggestions: list[dict]
    created_at: datetime

    class Config:
        from_attributes = True


class CompetitorSnapshotOut(BaseModel):
    id: int
    platform: str
    category: str
    gigs: list[dict]
    insights: list[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Profiles ----------

class ProfileTemplateIn(BaseModel):
    platform: Platform
    name: str
    pitch_template: str = ""


class ProfileTemplateOut(ProfileTemplateIn):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class PortfolioItemIn(BaseModel):
    title: str
    description: str = ""
    url: str = ""
    tags: list[str] = []


class PortfolioItemOut(PortfolioItemIn):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class RateCardIn(BaseModel):
    skill_category: str
    hourly_rate: Optional[float] = None
    fixed_min: Optional[float] = None
    currency: str = "USD"


class RateCardOut(RateCardIn):
    id: int

    class Config:
        from_attributes = True
