from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


# Use JSONB on Postgres; plain JSON elsewhere (e.g. sqlite for tests)
JSONType = JSON().with_variant(JSONB, "postgresql")


class KeywordGroup(Base):
    __tablename__ = "keyword_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    service_type: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    keywords: Mapped[list["Keyword"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("keyword_groups.id", ondelete="CASCADE"))
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # primary|secondary|negative
    weight: Mapped[float] = mapped_column(Float, default=1.0)

    group: Mapped[KeywordGroup] = relationship(back_populates="keywords")


class SearchFilter(Base):
    __tablename__ = "search_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    keyword_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("keyword_groups.id", ondelete="SET NULL"), nullable=True
    )
    platforms: Mapped[list] = mapped_column(JSONType, default=list)
    job_types: Mapped[list] = mapped_column(JSONType, default=list)
    budgets: Mapped[list] = mapped_column(JSONType, default=list)  # [{platform,min,max,currency}]
    experience_levels: Mapped[list] = mapped_column(JSONType, default=list)
    client_filters: Mapped[dict] = mapped_column(JSONType, default=dict)
    posted_within_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    apply_deadline_within_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    work_arrangements: Mapped[list] = mapped_column(JSONType, default=list)
    languages: Mapped[list] = mapped_column(JSONType, default=list)
    max_proposals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_threshold: Mapped[float] = mapped_column(Float, default=40.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    job_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    budget_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    budget_usd_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_usd_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    experience_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    client_info: Mapped[dict] = mapped_column(JSONType, default=dict)
    proposals_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skills: Mapped[list] = mapped_column(JSONType, default=list)
    languages: Mapped[list] = mapped_column(JSONType, default=list)
    work_arrangement: Mapped[str | None] = mapped_column(String(20), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    apply_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    quality_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    score_breakdown: Mapped[dict] = mapped_column(JSONType, default=dict)
    red_flags: Mapped[list] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new|notified|archived
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertSettings(Base):
    __tablename__ = "alert_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    realtime_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_score_alert: Mapped[float] = mapped_column(Float, default=70.0)
    digest_mode: Mapped[str] = mapped_column(String(10), default="off")  # off|hourly|daily
    hot_job_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    hot_job_max_proposals: Mapped[int] = mapped_column(Integer, default=5)
    hot_job_posted_hours: Mapped[int] = mapped_column(Integer, default=1)
    hot_job_min_score: Mapped[float] = mapped_column(Float, default=90.0)


class ProfileTemplate(Base):
    __tablename__ = "profile_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    pitch_template: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortfolioItem(Base):
    __tablename__ = "portfolio_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    tags: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RateCardEntry(Base):
    __tablename__ = "rate_card"

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_category: Mapped[str] = mapped_column(String(200), nullable=False)
    hourly_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    fixed_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="USD")


class AdapterCredential(Base):
    """Encrypted per-platform, per-principal credential blob.

    `principal` distinguishes e.g. the Upwork agency manager account from the
    freelancer account. `blob` is a Fernet-encrypted JSON object holding
    tokens/secrets; plaintext never touches the DB.
    """
    __tablename__ = "adapter_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    principal: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    blob: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet-encrypted JSON
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AdapterState(Base):
    """Key-value operational state per adapter (bid quotas, cursors, etc.)."""
    __tablename__ = "adapter_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[dict] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgencyAuditLog(Base):
    """Immutable audit trail for every Upwork agency-manager action."""
    __tablename__ = "agency_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)  # agency manager principal
    action: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. proposal.submit
    target: Mapped[str] = mapped_column(String(500), default="")  # job id / member id
    detail: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformAccount(Base):
    """A connected platform account (credentials themselves live in the vault).

    `credential_ref` points at the (platform, principal) pair in
    AdapterCredential — no secrets are stored here.
    """
    __tablename__ = "platform_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    principal: Mapped[str] = mapped_column(String(100), default="default")
    mode: Mapped[str] = mapped_column(String(20), default="api")  # api|stealth|hybrid|disabled
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    credential_ref: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SearchProfile(Base):
    """Named saved-search preset binding a keyword group + filter + platforms."""
    __tablename__ = "search_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    keyword_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("keyword_groups.id", ondelete="SET NULL"), nullable=True
    )
    filter_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_filters.id", ondelete="SET NULL"), nullable=True
    )
    boolean_query: Mapped[str] = mapped_column(String(1000), default="")
    auto_queue_proposals: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProposalQueueItem(Base):
    """AI-drafted proposals awaiting mandatory human review."""
    __tablename__ = "proposal_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    proposal_text: Mapped[str] = mapped_column(Text, default="")  # current working text
    humanized_text: Mapped[str] = mapped_column(Text, default="")  # stealth-typing version
    typing_plan: Mapped[list] = mapped_column(JSONType, default=list)  # typo/correction ops
    bid_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bid_rationale: Mapped[str] = mapped_column(String(500), default="")
    portfolio_item_ids: Mapped[list] = mapped_column(JSONType, default=list)
    portfolio_match: Mapped[dict] = mapped_column(JSONType, default=dict)  # id → overlap %
    template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis: Mapped[dict] = mapped_column(JSONType, default=dict)  # LLM job analysis
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)  # confidence < 50
    versions: Mapped[list] = mapped_column(JSONType, default=list)  # [{text,bid,by,at}]
    status: Mapped[str] = mapped_column(String(20), default="pending_review", index=True)
    # pending_review | generation_failed | approved | rejected | submitted | failed
    rejection_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rejection_notes: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | hired | rejected | ghosted
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_result: Mapped[dict] = mapped_column(JSONType, default=dict)
    request_type: Mapped[str] = mapped_column(String(30), default="job")  # job|buyer_request
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Template(Base):
    """Winning proposal template with outcome-driven win rate."""
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    tags: Mapped[list] = mapped_column(JSONType, default=list)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    source_proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RejectionFeedback(Base):
    """Why drafts get rejected — feeds prompt/temperature adjustments."""
    __tablename__ = "rejection_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal_queue.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    # too_generic | too_expensive | wrong_tone | overpromising | other
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GigTemplate(Base):
    __tablename__ = "gig_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    template_json: Mapped[dict] = mapped_column(JSONType, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)  # default: draft only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Gig(Base):
    """A gig listing created (or tracked) on a platform."""
    __tablename__ = "gigs"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("gig_templates.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[str] = mapped_column(String(300), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    # draft | active | paused
    url: Mapped[str] = mapped_column(String(1000), default="")
    price_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GigMetric(Base):
    """Weekly performance snapshot per gig."""
    __tablename__ = "gig_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    gig_id: Mapped[int] = mapped_column(ForeignKey("gigs.id", ondelete="CASCADE"), index=True)
    week: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO week, e.g. 2026-W32
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    suggestions: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CompetitorSnapshot(Base):
    __tablename__ = "competitor_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    gigs: Mapped[list] = mapped_column(JSONType, default=list)  # top-N competitor gig data
    insights: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StealthTask(Base):
    """Work queue for the external stealth-browser worker.

    Same handoff pattern as the Upwork agency pending_submissions: this
    service enqueues typed tasks; the browser pool worker (Playwright/
    Camoufox, fingerprint rotation, human behavior injection) polls
    `status=pending`, executes, and posts results back.
    """
    __tablename__ = "stealth_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # fiverr_create_gig | fiverr_fetch_buyer_requests | fiverr_send_offer
    # upwork_catalog_upsert | gig_scrape_metrics | competitor_scrape
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending | claimed | done | failed | skipped_circuit_open
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """General audit trail (proposals, gigs, buyer requests, LLM usage)."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # proposal_generated | proposal_approved | proposal_submitted |
    # gig_created | gig_published | buyer_request_sent | ...
    platform: Mapped[str] = mapped_column(String(30), default="")
    detail: Mapped[dict] = mapped_column(JSONType, default=dict)
    # llm_model, prompt_version, latency_ms, humanized, approved_by, platform_response...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
