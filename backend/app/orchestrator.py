"""Core orchestration: score → match profiles → draft proposal → review queue.

The auto-match pipeline (invoked from the ingest path):
    new job → quality score → passes thresholds → boolean/keyword profile match
    → search-filter gate → enqueue proposal generation (Celery task)
    → park in proposal_queue as pending_review → WebSocket notify.

The LLM call itself runs in `generate_and_queue`, off the request path
(Phase 2.2): ingest only runs the cheap gates inline and enqueues
`generate_proposal_task`. `maybe_queue_proposal` (gates + generation in one
call) stays directly callable for tests and for the task core.

Nothing here submits anything — submission is only possible via the review
queue endpoints, which require a human reviewer.
"""
import logging
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import platform_enabled
from .boolquery import evaluate, parse_boolean_query
from .filtering import job_matches_filter
from .models import (Job, PortfolioItem, ProposalQueueItem,
                     RateCardEntry, SearchFilter, SearchProfile)
from .schemas import JobOut
from .ws_manager import alerts

log = logging.getLogger(__name__)


@dataclass
class PipelineContext:
    """Per-ingest-call preload: everything the pipeline would otherwise
    re-query per job (kills the N+1 storm on bulk ingest).

    `profile_asts` is None when the user has no auto-queue search profiles
    (match-all semantics, unchanged); otherwise (profile, parsed AST) pairs.
    """
    rate_entries: list[RateCardEntry]
    portfolio_items: list[PortfolioItem]
    profile_asts: list[tuple[SearchProfile, object]] | None
    filters: dict[int, SearchFilter]


def build_pipeline_context(db: Session, user_id: int) -> PipelineContext:
    """Load rate card, portfolio, auto-queue profiles (+ parsed boolean ASTs)
    and their linked filters exactly once."""
    rate_entries = db.query(RateCardEntry).filter(RateCardEntry.user_id == user_id).all()
    portfolio_items = db.query(PortfolioItem).filter(PortfolioItem.user_id == user_id).all()
    profiles = (db.query(SearchProfile)
                .filter(SearchProfile.user_id == user_id,
                        SearchProfile.auto_queue_proposals.is_(True))
                .all())
    profile_asts = None
    filters: dict[int, SearchFilter] = {}
    if profiles:
        profile_asts = [(p, parse_boolean_query(p.boolean_query)) for p in profiles]
        filter_ids = {p.filter_id for p in profiles if p.filter_id}
        if filter_ids:
            filters = {f.id: f for f in db.query(SearchFilter)
                       .filter(SearchFilter.user_id == user_id,
                               SearchFilter.id.in_(filter_ids))
                       .all()}
    return PipelineContext(rate_entries=rate_entries,
                           portfolio_items=portfolio_items,
                           profile_asts=profile_asts,
                           filters=filters)


def select_portfolio_items(db: Session, user_id: int, job: Job, limit: int = 3,
                           items: list[PortfolioItem] | None = None) -> list[PortfolioItem]:
    """Auto-select portfolio pieces whose tags/title match the job's skills."""
    if items is None:
        items = db.query(PortfolioItem).filter(PortfolioItem.user_id == user_id).all()
    if not items:
        return []
    job_terms = " ".join([job.title or "", *(job.skills or [])]).lower()
    scored = []
    for item in items:
        hay = " ".join([item.title, *(item.tags or [])]).lower()
        score = max(
            (fuzz.token_set_ratio(skill.lower(), hay) for skill in (job.skills or [])),
            default=0,
        )
        if not (job.skills or []):
            score = fuzz.partial_ratio(item.title.lower(), job_terms)
        scored.append((score, item))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [item for score, item in scored[:limit] if score >= 50]


def pick_rate(db: Session, user_id: int, job: Job,
              entries: list[RateCardEntry] | None = None) -> RateCardEntry | None:
    """Best rate-card entry for the job's skills."""
    if entries is None:
        entries = db.query(RateCardEntry).filter(RateCardEntry.user_id == user_id).all()
    if not entries:
        return None
    best, best_score = None, 0
    for entry in entries:
        score = max(
            (fuzz.token_set_ratio(entry.skill_category.lower(), s.lower()) for s in (job.skills or [])),
            default=0,
        )
        score = max(score, fuzz.partial_ratio(entry.skill_category.lower(), (job.title or "").lower()))
        if score > best_score:
            best, best_score = entry, score
    return best


def _matching_profile_asts(ctx: PipelineContext, job: Job) -> list[SearchProfile] | None:
    """Boolean-query match against pre-parsed profile ASTs.

    Returns None when no auto-queue profiles exist (match all, unchanged);
    otherwise the profiles whose boolean query matches the job.
    """
    if ctx.profile_asts is None:
        return None
    text = f"{job.title}\n{job.description}"
    return [p for p, ast in ctx.profile_asts if evaluate(ast, text)]


def _matching_profiles(db: Session, user_id: int, job: Job) -> list[SearchProfile] | None:
    """Per-call variant of _matching_profile_asts (parses queries itself)."""
    profiles = (db.query(SearchProfile)
                .filter(SearchProfile.user_id == user_id,
                        SearchProfile.auto_queue_proposals.is_(True))
                .all())
    if not profiles:
        return None
    text = f"{job.title}\n{job.description}"
    return [p for p in profiles
            if evaluate(parse_boolean_query(p.boolean_query), text)]


def _passes_profile_filters(filters: dict[int, SearchFilter], job: Job,
                            profiles: list[SearchProfile]) -> bool:
    """A matched profile gates on its linked SearchFilter (when it has one).

    The job queues if at least one matching profile lets it through; profiles
    without a filter_id pass unconditionally.
    """
    for p in profiles:
        flt = filters.get(p.filter_id) if p.filter_id else None
        if flt is None:
            return True
        matched, reasons = job_matches_filter(job, flt)
        if matched:
            return True
        log.info("job %d blocked by filter '%s': %s", job.id, flt.name, "; ".join(reasons))
    return False


def generation_gates_pass(db: Session, job: Job, ctx: PipelineContext | None = None) -> bool:
    """Cheap inline checks (no LLM): archived/duplicate/negative-keyword/
    kill-switch/already-queued/profile match/filter gate."""
    if job.status == "archived":
        return False
    if (job.score_breakdown or {}).get("excluded_by_negative_keyword"):
        return False  # defense-in-depth for jobs ingested before exclusion archived
    if job.is_duplicate:
        return False  # duplicates don't each get a full LLM draft
    if not platform_enabled(db, job.user_id, job.platform):
        return False  # kill switch: no drafting for disabled platforms
    existing = (
        db.query(ProposalQueueItem)
        .filter(ProposalQueueItem.job_id == job.id,
                ProposalQueueItem.status.in_(
                    ["pending_review", "approved", "submitting", "submitted",
                     "queued_for_browser", "generation_failed"]))
        .first()
    )
    if existing:
        return False
    if ctx is not None:
        profiles = _matching_profile_asts(ctx, job)
        filters = ctx.filters
    else:
        profiles = _matching_profiles(db, job.user_id, job)
        filters = {}
        if profiles:
            filter_ids = {p.filter_id for p in profiles if p.filter_id}
            if filter_ids:
                filters = {f.id: f for f in db.query(SearchFilter)
                           .filter(SearchFilter.user_id == job.user_id,
                                   SearchFilter.id.in_(filter_ids))
                           .all()}
    if profiles is not None:
        if not profiles:
            return False
        if not _passes_profile_filters(filters, job, profiles):
            return False
    return True


def enqueue_generation_if_eligible(db: Session, job: Job,
                                   ctx: PipelineContext | None = None) -> bool:
    """Ingest path: run the gates inline, hand the LLM work to a Celery task.

    Broker-down degrades to "no generation" (logged) — the request path must
    never block on (or crash because of) LLM work.
    """
    if not generation_gates_pass(db, job, ctx):
        return False
    from .tasks import generate_proposal_task
    try:
        generate_proposal_task.delay(job.id)
    except Exception as exc:  # noqa: BLE001 — broker unavailable
        log.warning("generation enqueue failed for job %d (%s); left ungenerated",
                    job.id, exc)
        return False
    return True


async def maybe_queue_proposal(db: Session, job: Job,
                               ctx: PipelineContext | None = None) -> ProposalQueueItem | None:
    """Queue a drafted proposal if the job clears the pipeline gates."""
    if not generation_gates_pass(db, job, ctx):
        return None
    return await generate_and_queue(db, job, ctx=ctx)


async def regenerate_failed_item(db: Session, item: ProposalQueueItem,
                                 ctx: PipelineContext | None = None) -> ProposalQueueItem | None:
    """Re-run generation for a generation_failed queue item, reusing the row
    (bounded retry path — see the generation_retry beat)."""
    job = db.get(Job, item.job_id)
    if job is None or job.status == "archived":
        return None
    return await generate_and_queue(db, job, ctx=ctx, item=item)


def _commit_generation_item(db: Session, job: Job,
                            item: ProposalQueueItem) -> tuple[ProposalQueueItem | None, bool]:
    """Commit a generated queue item, tolerating a lost insert race.

    generation_gates_pass is select-then-insert: two concurrent generations
    for the same job can both pass. The partial unique index
    (uq_proposal_queue_live_job) makes the loser fail here instead of
    double-inserting. Returns (row, committed): on a lost race, committed is
    False and row is the concurrently-inserted winner (the loser must skip
    its own broadcast — the winner already sent one).
    """
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        log.info("generation for job %d raced a concurrent insert; "
                 "keeping the existing queue item", job.id)
        return ((db.query(ProposalQueueItem)
                 .filter(ProposalQueueItem.job_id == job.id,
                         ProposalQueueItem.request_type == "job",
                         ProposalQueueItem.status.notin_(["rejected", "failed"]))
                 .order_by(ProposalQueueItem.created_at.desc())
                 .first()), False)
    db.refresh(item)
    return item, True


async def generate_and_queue(db: Session, job: Job, ctx: PipelineContext | None = None,
                             item: ProposalQueueItem | None = None) -> ProposalQueueItem | None:
    """The LLM path: tuning → few-shot → generate → persist → WS notify.

    Runs in the generate_proposal_task Celery task (off the request path).
    With `item` set, regenerates that row in place instead of inserting one.
    Returns None only when a lost insert race left no row to report.
    """
    from . import proposal_gen
    from .models import AuditLog
    from .templates import generation_tuning, top_templates

    tuning = generation_tuning(db, job.user_id, job.platform)
    few_shot = top_templates(db, job.user_id, job.platform, job.skills or [])
    try:
        gen = await proposal_gen.generate(
            db, job, few_shot=few_shot, temperature=tuning["temperature"],
            prompt_hints=tuning["prompt_hints"], ctx=ctx,
        )
    except Exception as exc:  # noqa: BLE001 — LLM timeout/rate limit/etc.
        log.exception("proposal generation failed for job %d", job.id)
        if item is None:
            item = ProposalQueueItem(user_id=job.user_id, job_id=job.id, platform=job.platform,
                                     status="generation_failed", needs_review=True)
            db.add(item)
        item.submission_result = {**(item.submission_result or {}), "error": str(exc)}
        item, committed = _commit_generation_item(db, job, item)
        if item is None:  # raced row already cleaned up — nothing to report
            return None
        if not committed:  # lost race — the winner already broadcast
            return item
        await alerts.broadcast(job.user_id, {
            "type": "generation_failed", "proposal_id": item.id,
            "error": str(exc), "job_id": job.id,
        })
        return item

    if item is None:
        item = ProposalQueueItem(user_id=job.user_id, job_id=job.id, platform=job.platform)
        db.add(item)
    item.proposal_text = gen["humanized_text"] or gen["draft_text"]
    item.humanized_text = gen["humanized_text"]
    item.typing_plan = gen["typing_plan"]
    item.bid_amount = gen["bid_amount"]
    item.bid_period_days = gen["bid_period_days"]
    item.bid_rationale = gen["bid_rationale"]
    item.portfolio_item_ids = gen["portfolio_item_ids"]
    item.portfolio_match = gen["portfolio_match"]
    item.analysis = gen["analysis"]
    item.confidence = gen["confidence"]
    item.needs_review = gen["needs_review"]
    from .client_intel import compute_bid_advice
    item.bid_advice = compute_bid_advice(job)  # go/no-go market intel (None when unknown)
    item.submission_result = ({"warning": gen["leak_warning"]} if gen.get("leak_warning") else {})
    item.versions = (list(item.versions or []) if item.versions else
                     []) + [{"text": gen["draft_text"], "bid": gen["bid_amount"],
                             "by": "generator", "at": utcnow_iso()}]
    item.status = "pending_review"
    db.add(AuditLog(user_id=job.user_id, action_type="proposal_generated", platform=job.platform, detail={
        "job_id": job.id, "llm_model": gen["llm_model"],
        "prompt_version": proposal_gen_llm_version(),
        "latency_ms": gen["latency_ms"], "humanized": bool(gen["humanized_text"]),
        "confidence": gen["confidence"],
    }))
    item, committed = _commit_generation_item(db, job, item)
    if item is None:  # raced row already cleaned up — nothing to report
        return None
    if not committed:  # lost race — the winner already broadcast
        return item
    await alerts.broadcast(job.user_id, {
        "type": "proposal_queued",
        "proposal_id": item.id,
        "job": JobOut.model_validate(job).model_dump(mode="json"),
    })
    log.info("queued proposal %d for job %d (%s) in %dms",
             item.id, job.id, job.title[:60], gen["latency_ms"])
    return item


def utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def proposal_gen_llm_version() -> str:
    from .llm import PROMPT_VERSION
    return PROMPT_VERSION
