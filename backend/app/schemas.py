from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class BoundedModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def bound_payload(cls, data):
        def check(value, depth=0):
            if depth > 12:
                raise ValueError("payload is too deeply nested")
            if isinstance(value, str) and len(value) > 50000:
                raise ValueError("text exceeds 50000 characters")
            if isinstance(value, (list, dict)):
                if len(value) > 1000:
                    raise ValueError("collection exceeds 1000 entries")
                for child in (value.values() if isinstance(value, dict) else value):
                    check(child, depth + 1)
        check(data)
        return data


# Canonical platform sets live in app/platforms.py (ALL_PLATFORMS mirrors this
# Literal; a test keeps them in sync). "indeed" is accepted for forward-compat
# but served by no subsystem yet.
Platform = Literal["upwork", "fiverr", "freelancer", "peopleperhour", "guru", "linkedin", "indeed"]
KeywordKind = Literal["primary", "secondary", "negative"]
JobType = Literal["fixed", "hourly", "retainer", "contest", "gig", "annual"]
ExperienceLevel = Literal["entry", "intermediate", "expert"]
WorkArrangement = Literal["remote", "onsite", "hybrid"]


# ---------- Auth ----------

class UserOut(BoundedModel):
    id: int
    email: str
    display_name: str
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RegisterIn(BoundedModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=72)  # 72 = bcrypt input limit
    display_name: str = Field(default="", max_length=200)

    @field_validator("email")
    @classmethod
    def canonical_email(cls, value: str) -> str:
        from .email_identity import normalize_email
        return normalize_email(value)

    @field_validator("password")
    @classmethod
    def password_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return value


class LoginIn(BoundedModel):
    email: str
    password: str


class TokenOut(BoundedModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class PasswordChangeIn(BoundedModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)  # 72 = bcrypt input limit


    @field_validator("new_password")
    @classmethod
    def password_byte_limit(cls, value: str) -> str:
        return RegisterIn.password_byte_limit(value)


class SubmissionReconcileIn(BoundedModel):
    submitted: bool
    evidence: str = Field(min_length=10, max_length=2000)


class AccountDeleteIn(BoundedModel):
    password: str


# ---------- Keywords ----------

class KeywordIn(BoundedModel):
    term: str
    kind: KeywordKind
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class KeywordOut(KeywordIn):
    id: int

    model_config = ConfigDict(from_attributes=True)


class KeywordGroupIn(BoundedModel):
    name: str
    service_type: str = ""
    keywords: list[KeywordIn] = []


class KeywordGroupOut(BoundedModel):
    id: int
    name: str
    service_type: str
    created_at: datetime
    keywords: list[KeywordOut] = []

    model_config = ConfigDict(from_attributes=True)


# ---------- Search filters ----------

class ClientFilters(BoundedModel):
    payment_verified: Optional[bool] = None
    min_hire_rate: Optional[float] = None
    min_total_spent: Optional[float] = None
    countries: list[str] = []


class PlatformBudget(BoundedModel):
    platform: Platform
    min: Optional[float] = None
    max: Optional[float] = None
    currency: str = "USD"


class SearchFilterIn(BoundedModel):
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

    model_config = ConfigDict(from_attributes=True)


# ---------- Jobs ----------

class ClientInfo(BoundedModel):
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


class JobIngest(BoundedModel):
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


class IngestJobsIn(BoundedModel):
    jobs: list[JobIngest] = []


class ClientHistoryOut(BoundedModel):
    past_proposals: int
    hired: int
    rejected: int
    ghosted: int


class JobOut(BoundedModel):
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

    model_config = ConfigDict(from_attributes=True)


class IngestResult(BoundedModel):
    ingested: int
    auto_archived: int
    alerts_sent: int


class BulkArchiveAction(BoundedModel):
    ids: list[int]


class ScorePreviewIn(BoundedModel):
    job: JobIngest


class ScorePreviewOut(BoundedModel):
    quality_score: float
    score_breakdown: dict
    red_flags: list[str]


class PreviewResult(BoundedModel):
    matched: list[JobOut]
    excluded_count: int


# ---------- Alerts ----------

class AlertSettingsSchema(BoundedModel):
    realtime_enabled: bool = True
    min_score_alert: float = Field(default=70.0, ge=0, le=100)
    digest_mode: Literal["off", "hourly", "daily"] = "off"
    hot_job_enabled: bool = True
    hot_job_max_proposals: int = 5        # hot = <5 proposals
    hot_job_posted_hours: int = 1         # posted <1 hour ago
    hot_job_min_score: float = Field(default=90.0, ge=0, le=100)  # and score >90

    model_config = ConfigDict(from_attributes=True)


# ---------- Orchestration: search profiles, accounts, proposal queue ----------

class SearchProfileIn(BoundedModel):
    name: str
    keyword_group_id: Optional[int] = None
    filter_id: Optional[int] = None
    boolean_query: str = ""               # e.g. "(React OR Next.js) AND (NOT WordPress)"
    auto_queue_proposals: bool = True


class SearchProfileOut(SearchProfileIn):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PlatformAccountIn(BoundedModel):
    platform: Platform
    label: str = Field(min_length=1, max_length=200)
    principal: str = Field(default="default", pattern=r"^[A-Za-z0-9_.@-]{1,100}$")
    mode: Literal["api", "stealth", "hybrid", "disabled"] = "api"
    enabled: bool = True
    credential_ref: str = ""
    # recognized keys: bidder_id (freelancer user id), on_behalf_of (upwork agency member),
    # proxy_url (per-account worker proxy — see worker/README.md)
    settings: dict = {}


class PlatformAccountOut(PlatformAccountIn):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CredentialsIn(BoundedModel):
    """Secret key/value pairs to store in the vault for a platform account.

    Recognized keys per platform (others are rejected 422):
      freelancer/upwork: access_token (+ optional refresh_token)
      stealth platforms: storage_state_json (Playwright storage_state string)
                         OR username + password (fallback-only login)
    """
    secrets: dict[str, str]


class CredentialStatusOut(BoundedModel):
    enrolled: bool
    keys: list[str]
    updated_at: Optional[datetime] = None


class OAuthCompleteIn(BoundedModel):
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=20, max_length=200)
    redirect_uri: Optional[str] = None  # defaults to FREELANCER_REDIRECT_URI


class BidAdviceOut(BoundedModel):
    recommendation: Literal["bid", "caution", "skip"]
    reason: str


class ProposalQueueOut(BoundedModel):
    revision: int = 1
    approved_snapshot: Optional[dict] = None
    platform_account_id: Optional[int] = None
    id: int
    job_id: int
    platform: str
    proposal_text: str
    humanized_text: Optional[str] = None
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
    submitted_at: Optional[datetime] = None
    outcome_at: Optional[datetime] = None
    reviewed_at: Optional[datetime]
    client_replied_at: Optional[datetime] = None
    submission_result: dict
    created_at: datetime
    job: Optional[JobOut] = None

    model_config = ConfigDict(from_attributes=True)


class ProposalReviewAction(BoundedModel):
    platform_account_id: Optional[int] = Field(default=None, ge=1)
    expected_revision: int = Field(ge=1)
    reviewer: str
    proposal_text: Optional[str] = None   # reviewer may edit before approving
    bid_amount: Optional[float] = None
    bid_period_days: Optional[int] = None
    # reviewer picked an existing template from suggestions: reuse it on
    # approve instead of minting a new one
    template_id: Optional[int] = None
    # set False to skip minting a Template from this approval
    save_as_template: bool = True


class ProposalRejectAction(BoundedModel):
    reviewer: str
    reason: Literal["too_generic", "too_expensive", "wrong_tone", "overpromising", "other"] = "other"
    notes: str = ""


class BulkApproveAction(BoundedModel):
    expected_revisions: dict[int, int]
    ids: list[int]
    reviewer: str


class OutcomeAction(BoundedModel):
    outcome: Literal["hired", "rejected", "ghosted"]


class MarkSubmittedAction(BoundedModel):
    channel: Optional[str] = None


class InterviewQuestion(BoundedModel):
    question: str
    suggested_answer: str


class InterviewPrepOut(BoundedModel):
    questions: list[InterviewQuestion]
    pain_points: list[str] = []
    red_flags: list[str] = []
    talking_points: list[str] = []


# ---------- Proposal generation v3 / templates / gigs ----------

class TemplateOut(BoundedModel):
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

    model_config = ConfigDict(from_attributes=True)


class GigTemplateIn(BoundedModel):
    platform: Platform
    name: str
    template_json: dict = {}
    auto_publish: bool = False


class GigTemplateOut(GigTemplateIn):
    id: int
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GigOut(BoundedModel):
    id: int
    account_id: int | None = None
    account_binding_version: int = 0
    platform: str
    template_id: Optional[int]
    external_id: str
    title: str
    status: str
    url: str
    price_min: Optional[float]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StealthTaskClaimIn(BoundedModel):
    worker_id: str


class GigMetricIn(BoundedModel):
    gig_id: int
    impressions: Optional[int] = Field(default=None, ge=0)
    clicks: Optional[int] = Field(default=None, ge=0)
    orders: Optional[int] = Field(default=None, ge=0)
    revenue: Optional[float] = Field(default=None, ge=0)
    week: Optional[str] = Field(default=None, pattern=r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")
    task_id: Optional[int] = None
    worker_id: Optional[str] = None
    claim_token: Optional[str] = None


class GigMetricOut(GigMetricIn):
    id: int
    suggestions: list[dict]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CompetitorSnapshotOut(BoundedModel):
    id: int
    platform: str
    category: str
    gigs: list[dict]
    insights: list[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Profiles ----------

class ProfileTemplateIn(BoundedModel):
    platform: Platform
    name: str
    pitch_template: str = ""


class ProfileTemplateOut(ProfileTemplateIn):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PortfolioItemIn(BoundedModel):
    title: str
    description: str = ""
    url: str = ""
    tags: list[str] = []


class PortfolioItemOut(PortfolioItemIn):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RateCardIn(BoundedModel):
    skill_category: str
    hourly_rate: Optional[float] = None
    fixed_min: Optional[float] = None
    currency: str = "USD"


class RateCardOut(RateCardIn):
    id: int

    model_config = ConfigDict(from_attributes=True)


class SeoTitleIn(BoundedModel):
    title: str = Field(default="", max_length=500)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class FaqGenerateIn(BoundedModel):
    gig_type: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=500)
    count: int = Field(default=4, ge=1, le=20)


class GigAccountIn(BoundedModel):
    account_id: int | None = Field(default=None, ge=1)
    expected_version: int = Field(ge=0)


class GigRegisterIn(BoundedModel):
    platform: Platform
    account_id: int | None = Field(default=None, ge=1)
    title: str = Field(default="", max_length=300)
    external_id: str = Field(default="", max_length=300)
    url: str = Field(default="", max_length=1000)
    status: Literal["draft", "active", "paused", "deleted"] = "draft"
    price_min: float | None = Field(default=None, ge=0, le=100000000)
    template_id: int | None = Field(default=None, ge=1)

    @field_validator("url")
    @classmethod
    def listing_url(cls, value):
        if value:
            parsed = urlparse(value)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
                raise ValueError("listing URL must be an absolute http or https URL")
        return value


class TemplateGenerateIn(BoundedModel):
    platform: Platform = "upwork"
    title: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=10000)
    tone: str = Field(default="", max_length=1000)
    skills: list[str] = Field(default_factory=list, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=4096)
    timeout: float | None = Field(default=None, gt=0, le=120)
    save: bool = False


class BooleanValidateIn(BoundedModel):
    query: str = Field(default="", max_length=10000)


class AgencyMemberIn(BoundedModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
