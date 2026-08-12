"""Core orchestration: score → match profiles → draft proposal → review queue.

The auto-match pipeline (invoked from the ingest path):
    new job → quality score → passes thresholds → boolean/keyword profile match
    → generate proposal draft (template + rate card + auto-selected portfolio)
    → park in proposal_queue as pending_review → WebSocket notify.

Nothing here submits anything — submission is only possible via the review
queue endpoints, which require a human reviewer.
"""
import logging

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from .boolquery import matches_boolean_query
from .models import (Job, PortfolioItem, ProfileTemplate, ProposalQueueItem,
                     RateCardEntry, SearchProfile)
from .schemas import JobOut
from .ws_manager import alerts

log = logging.getLogger(__name__)

GENERIC_TEMPLATE = (
    "Hi — I read your post \"{job_title}\" and it maps well to my work in {skills}.\n\n"
    "Relevant work: {portfolio_links}\n"
    "My rate for this kind of project: {rate_line}\n\n"
    "Happy to share a concrete plan for your deliverables before you commit.\n"
    "— {sender_name}"
)


def select_portfolio_items(db: Session, job: Job, limit: int = 3) -> list[PortfolioItem]:
    """Auto-select portfolio pieces whose tags/title match the job's skills."""
    items = db.query(PortfolioItem).all()
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


def pick_rate(db: Session, job: Job) -> RateCardEntry | None:
    """Best rate-card entry for the job's skills."""
    entries = db.query(RateCardEntry).all()
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


def _render(template: str, job: Job, rate: RateCardEntry | None,
            portfolio: list[PortfolioItem], sender_name: str) -> str:
    skills = ", ".join((job.skills or [])[:5]) or "this stack"
    if portfolio:
        links = "; ".join(f"{p.title} ({p.url})" if p.url else p.title for p in portfolio)
    else:
        links = "available on request"
    if rate:
        parts = []
        if rate.hourly_rate:
            parts.append(f"${rate.hourly_rate:g}/hr")
        if rate.fixed_min:
            parts.append(f"fixed projects from ${rate.fixed_min:g}")
        rate_line = " / ".join(parts) + f" ({rate.currency})"
    elif job.budget_usd_max:
        rate_line = f"within your stated budget (up to ${job.budget_usd_max:g})"
    else:
        rate_line = "happy to discuss a fair rate"
    return template.format(
        job_title=job.title,
        client_country=(job.client_info or {}).get("country") or "your location",
        skills=skills,
        portfolio_links=links,
        rate_line=rate_line,
        platform=job.platform,
        sender_name=sender_name,
    )


def generate_proposal(db: Session, job: Job, sender_name: str = "GigHound user") -> dict:
    """Draft a proposal for a job. Returns dict of ProposalQueueItem fields."""
    rate = pick_rate(db, job)
    portfolio = select_portfolio_items(db, job)
    tpl = (
        db.query(ProfileTemplate)
        .filter(ProfileTemplate.platform == job.platform)
        .order_by(ProfileTemplate.created_at)
        .first()
    )
    template_text = tpl.pitch_template if tpl and tpl.pitch_template else GENERIC_TEMPLATE
    text = _render(template_text, job, rate, portfolio, sender_name)

    bid_amount = None
    if job.job_type == "fixed":
        if job.budget_usd_min and job.budget_usd_max:
            bid_amount = round((job.budget_usd_min + job.budget_usd_max) / 2, 2)
        elif rate and rate.fixed_min:
            bid_amount = rate.fixed_min
    elif job.job_type == "hourly" and rate and rate.hourly_rate:
        bid_amount = rate.hourly_rate

    return {
        "job_id": job.id,
        "platform": job.platform,
        "proposal_text": text,
        "bid_amount": bid_amount,
        "bid_period_days": 14 if job.job_type == "fixed" else None,
        "portfolio_item_ids": [p.id for p in portfolio],
        "template_id": tpl.id if tpl else None,
    }


def _matches_any_profile(db: Session, job: Job) -> bool:
    """Boolean-query match against saved search profiles (empty set = match all)."""
    profiles = db.query(SearchProfile).filter(SearchProfile.auto_queue_proposals.is_(True)).all()
    if not profiles:
        return True
    text = f"{job.title}\n{job.description}"
    return any(matches_boolean_query(p.boolean_query, text) for p in profiles)


async def maybe_queue_proposal(db: Session, job: Job) -> ProposalQueueItem | None:
    """Queue a drafted proposal if the job clears the pipeline gates."""
    if job.status == "archived":
        return None
    existing = (
        db.query(ProposalQueueItem)
        .filter(ProposalQueueItem.job_id == job.id,
                ProposalQueueItem.status.in_(
                    ["pending_review", "approved", "submitted", "generation_failed"]))
        .first()
    )
    if existing:
        return None
    if not _matches_any_profile(db, job):
        return None

    from . import proposal_gen
    from .models import AuditLog
    from .templates import generation_tuning, top_templates

    tuning = generation_tuning(db, job.platform)
    few_shot = top_templates(db, job.platform, job.skills or [])
    try:
        gen = await proposal_gen.generate(
            db, job, few_shot=few_shot, temperature=tuning["temperature"]
        )
    except Exception as exc:  # noqa: BLE001 — LLM timeout/rate limit/etc.
        log.exception("proposal generation failed for job %d", job.id)
        item = ProposalQueueItem(job_id=job.id, platform=job.platform,
                                 status="generation_failed", needs_review=True,
                                 submission_result={"error": str(exc)})
        db.add(item)
        db.commit()
        db.refresh(item)
        await alerts.broadcast({
            "type": "generation_failed", "proposal_id": item.id,
            "error": str(exc), "job_id": job.id,
        })
        return item

    item = ProposalQueueItem(
        job_id=job.id,
        platform=job.platform,
        proposal_text=gen["humanized_text"] or gen["draft_text"],
        humanized_text=gen["humanized_text"],
        typing_plan=gen["typing_plan"],
        bid_amount=gen["bid_amount"],
        bid_period_days=gen["bid_period_days"],
        bid_rationale=gen["bid_rationale"],
        portfolio_item_ids=gen["portfolio_item_ids"],
        portfolio_match=gen["portfolio_match"],
        analysis=gen["analysis"],
        confidence=gen["confidence"],
        needs_review=gen["needs_review"],
        versions=[{"text": gen["draft_text"], "bid": gen["bid_amount"],
                   "by": "generator", "at": utcnow_iso()}],
        status="pending_review",
    )
    db.add(item)
    db.add(AuditLog(action_type="proposal_generated", platform=job.platform, detail={
        "job_id": job.id, "llm_model": gen["llm_model"],
        "prompt_version": proposal_gen_llm_version(),
        "latency_ms": gen["latency_ms"], "humanized": bool(gen["humanized_text"]),
        "confidence": gen["confidence"],
    }))
    db.commit()
    db.refresh(item)
    await alerts.broadcast({
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
