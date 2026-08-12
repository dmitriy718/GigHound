"""Template library, win-rate tracking, and rejection learning.

Every approved proposal becomes a Template. Outcomes (hired/rejected/ghosted)
update win rates. Rejection feedback adjusts generation temperature and
prompt emphasis per platform. Top templates become few-shot examples for
future generations.
"""
import logging

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from .models import ProposalQueueItem, RejectionFeedback, Template

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
                     tags: list[str] | None = None) -> Template:
    """Snapshot an approved proposal into the template library."""
    tpl = Template(
        title=title or (proposal.proposal_text[:60] or f"Proposal #{proposal.id}"),
        platform=proposal.platform,
        text=proposal.proposal_text,
        bid=proposal.bid_amount,
        tags=tags or [],
        source_proposal_id=proposal.id,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


def record_outcome(db: Session, proposal: ProposalQueueItem, outcome: str) -> None:
    """outcome: hired | rejected | ghosted — updates template win rates."""
    proposal.outcome = outcome
    tpl = None
    if proposal.template_id:
        tpl = db.get(Template, proposal.template_id)
    if tpl is None:
        tpl = (db.query(Template)
               .filter(Template.source_proposal_id == proposal.id)
               .first())
    if tpl:
        tpl.uses += 1
        if outcome == "hired":
            tpl.wins += 1
        elif outcome in ("rejected", "ghosted"):
            tpl.losses += 1
        total = tpl.wins + tpl.losses
        tpl.win_rate = round(100 * tpl.wins / total, 1) if total else 0.0
    db.commit()


def record_rejection(db: Session, proposal: ProposalQueueItem, reason: str,
                     notes: str = "") -> RejectionFeedback:
    fb = RejectionFeedback(
        proposal_id=proposal.id, platform=proposal.platform,
        reason=reason if reason in _REASON_EFFECTS else "other", notes=notes,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return fb


def generation_tuning(db: Session, platform: str) -> dict:
    """Temperature + prompt hints derived from recent rejection trends."""
    recent = (
        db.query(RejectionFeedback)
        .filter(RejectionFeedback.platform == platform)
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


def top_templates(db: Session, platform: str, skills: list[str] | None = None,
                  limit: int = 3) -> list[Template]:
    """Best templates for few-shot prompting: platform + skill overlap + win rate."""
    candidates = (
        db.query(Template)
        .filter(Template.platform == platform, Template.uses >= 0)
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
    return candidates[:limit]
