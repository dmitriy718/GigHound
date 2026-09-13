"""Template library, win-rate tracking, and rejection learning.

Every approved proposal becomes a Template. Outcomes (hired/rejected/ghosted)
update win rates. Rejection feedback adjusts generation temperature and
prompt emphasis per platform. Top templates become few-shot examples for
future generations.
"""
import logging
from datetime import datetime, timezone

from rapidfuzz import fuzz
from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import Job, ProposalQueueItem, RejectionFeedback, Template

log = logging.getLogger(__name__)

# rejection-reason → generation adjustments
_REASON_EFFECTS = {
    "too_generic":    {"temperature": -0.15, "prompt_hint": "Reference more job-specific details."},
    "too_expensive":  {"temperature": 0.0,   "prompt_hint": "Keep bid near the client's stated budget."},
    "wrong_tone":     {"temperature": -0.1,  "prompt_hint": "Match the client's tone more closely."},
    "overpromising":  {"temperature": -0.2,  "prompt_hint": "Only claim skills explicitly listed as strengths."},
    "other":          {"temperature": 0.0,   "prompt_hint": ""},
}

_BASE_TEMPERATURE = 0.7


def save_as_template(db: Session, proposal: ProposalQueueItem, title: str | None = None,
                     tags: list[str] | None = None, *, commit: bool = True) -> Template:
    """Snapshot an approved proposal into the template library."""
    tpl = Template(
        user_id=proposal.user_id,
        title=title or (proposal.proposal_text[:60] or f"Proposal #{proposal.id}"),
        platform=proposal.platform,
        text=proposal.proposal_text,
        bid=proposal.bid_amount,
        tags=tags or [],
        source_proposal_id=proposal.id,
    )
    db.add(tpl)
    if commit:
        db.commit()
        db.refresh(tpl)
    else:
        db.flush()
    return tpl


def template_for_approval(db: Session, proposal: ProposalQueueItem) -> Template | None:
    """Template provenance on approve (Phase 2.5).

    If the reviewer started from a suggested template (`template_id` set and
    resolvable), link THAT template — do not mint a new one. Only mint a new
    Template when the item has no template_id and the item's
    `save_as_template` flag is on (reviewer opt-out).

    A use is counted once when a reviewer approves this template selection.
    """
    if proposal.template_id:
        tpl = db.get(Template, proposal.template_id)
        if tpl is not None and tpl.user_id == proposal.user_id:
            db.execute(update(Template).where(Template.id == tpl.id).values(uses=Template.uses + 1))
            return tpl
    if not proposal.save_as_template:
        return None
    return save_as_template(db, proposal, commit=False)


def record_outcome(db: Session, proposal: ProposalQueueItem, outcome: str, *, commit: bool = True) -> bool:
    """Idempotent current outcome, serialized with corrections and derived totals."""
    from sqlalchemy import func, or_
    from .models import AuditLog
    if outcome not in ("hired", "rejected", "ghosted"):
        raise ValueError("unsupported outcome")
    db.refresh(proposal, with_for_update=True)
    if proposal.status != "submitted":
        raise ValueError("only confirmed submitted proposals can have outcomes")
    previous = proposal.outcome
    if previous == outcome:
        return False
    tpl = None
    if proposal.template_id:
        tpl = db.query(Template).filter_by(id=proposal.template_id, user_id=proposal.user_id).with_for_update().one_or_none()
    if tpl is None:
        tpl = db.query(Template).filter_by(user_id=proposal.user_id, source_proposal_id=proposal.id).with_for_update().first()
    proposal.outcome = outcome
    proposal.outcome_at = datetime.now(timezone.utc)
    proposal.submission_result = {**(proposal.submission_result or {}),
                                 "outcome_recorded_at": proposal.outcome_at.isoformat()}
    db.add(AuditLog(user_id=proposal.user_id, action_type="proposal_outcome", platform=proposal.platform,
                    detail={"proposal_id": proposal.id, "previous": previous, "outcome": outcome}))
    db.flush()
    if tpl:
        rows = db.query(ProposalQueueItem.outcome, func.count()).filter(
            ProposalQueueItem.user_id == proposal.user_id, ProposalQueueItem.status == "submitted",
            or_(ProposalQueueItem.template_id == tpl.id, ProposalQueueItem.id == tpl.source_proposal_id),
        ).group_by(ProposalQueueItem.outcome).all()
        counts = dict(rows)
        tpl.wins = counts.get("hired", 0)
        tpl.losses = counts.get("rejected", 0) + counts.get("ghosted", 0)
        total = tpl.wins + tpl.losses
        tpl.win_rate = round(100 * tpl.wins / total, 1) if total else 0.0
    if commit:
        db.commit()
    return True


def record_rejection(db: Session, proposal: ProposalQueueItem, reason: str,
                     notes: str = "", *, commit: bool = True) -> RejectionFeedback:
    fb = RejectionFeedback(
        user_id=proposal.user_id,
        proposal_id=proposal.id, platform=proposal.platform,
        reason=reason if reason in _REASON_EFFECTS else "other", notes=notes,
    )
    db.add(fb)
    if commit:
        db.commit()
        db.refresh(fb)
    else:
        db.flush()
    return fb


def generation_tuning(db: Session, user_id: int, platform: str) -> dict:
    """Temperature + prompt hints derived from recent rejection trends."""
    recent = (
        db.query(RejectionFeedback)
        .filter(RejectionFeedback.user_id == user_id,
                RejectionFeedback.platform == platform)
        .order_by(RejectionFeedback.created_at.desc())
        .limit(20)
        .all()
    )
    temperature = _BASE_TEMPERATURE
    hints = []
    for fb in recent:
        fx = _REASON_EFFECTS[fb.reason]
        temperature += fx["temperature"] * 0.3  # damped accumulation
        if fx["prompt_hint"] and fx["prompt_hint"] not in hints:
            hints.append(fx["prompt_hint"])
    return {
        "temperature": round(max(0.2, min(1.2, temperature)), 2),
        "prompt_hints": hints,
        "samples": len(recent),
    }


def top_templates(db: Session, user_id: int, platform: str, skills: list[str] | None = None,
                  limit: int = 3) -> list[Template]:
    """Best templates for few-shot prompting: platform + skill overlap + win rate.

    Suggestions are read-only. Uses are counted on human selection approval.
    """
    candidates = (
        db.query(Template)
        .filter(Template.user_id == user_id,
                Template.platform == platform)
        .order_by(Template.win_rate.desc(), Template.id.desc()).limit(500)
        .all()
    )
    skills = [s.lower() for s in (skills or [])]

    def score(t: Template) -> float:
        overlap = max(
            (fuzz.token_set_ratio(s, " ".join(t.tags).lower() + " " + t.title.lower())
             for s in skills),
            default=0,
        )
        return t.win_rate + 0.3 * overlap + min(10, t.uses)  # experience bonus

    candidates.sort(key=score, reverse=True)
    selected = candidates[:limit]
    return selected
