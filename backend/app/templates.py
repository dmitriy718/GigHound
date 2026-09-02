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
                     tags: list[str] | None = None) -> Template:
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
    db.commit()
    db.refresh(tpl)
    return tpl


def template_for_approval(db: Session, proposal: ProposalQueueItem) -> Template | None:
    """Template provenance on approve (Phase 2.5).

    If the reviewer started from a suggested template (`template_id` set and
    resolvable), link THAT template — do not mint a new one. Only mint a new
    Template when the item has no template_id and the item's
    `save_as_template` flag is on (reviewer opt-out).

    `uses` is NOT incremented here — a use is counted only at selection time
    (`top_templates`), so one proposal contributes at most one use.
    """
    if proposal.template_id:
        tpl = db.get(Template, proposal.template_id)
        if tpl is not None:
            return tpl
    if not proposal.save_as_template:
        return None
    return save_as_template(db, proposal)


def record_outcome(db: Session, proposal: ProposalQueueItem, outcome: str) -> None:
    """outcome: hired | rejected | ghosted — updates template win rates."""
    proposal.outcome = outcome
    # stamped once (idempotent re-marks keep the first timestamp) so the
    # analytics trend can bucket outcomes by ISO week — there is no
    # dedicated outcome timestamp column
    result = dict(proposal.submission_result or {})
    result.setdefault("outcome_recorded_at",
                      datetime.now(timezone.utc).isoformat())
    proposal.submission_result = result
    tpl = None
    if proposal.template_id:
        tpl = db.get(Template, proposal.template_id)
    if tpl is None:
        tpl = (db.query(Template)
               .filter(Template.user_id == proposal.user_id,
                       Template.source_proposal_id == proposal.id)
               .first())
    if tpl:
        # SQL atomic increments: concurrent outcome syncs must not lose
        # updates to a read-modify-write race
        if outcome == "hired":
            db.execute(update(Template).where(Template.id == tpl.id)
                       .values(wins=Template.wins + 1))
        elif outcome in ("rejected", "ghosted"):
            db.execute(update(Template).where(Template.id == tpl.id)
                       .values(losses=Template.losses + 1))
        db.flush()
        db.refresh(tpl)  # pick up the atomic increments for win_rate
        total = tpl.wins + tpl.losses
        tpl.win_rate = round(100 * tpl.wins / total, 1) if total else 0.0
    if outcome == "hired" and proposal.bid_amount:
        # won-bid learning: the winning amount feeds future bid suggestions
        # for the matched rate-card category (Phase 3.4)
        from .orchestrator import pick_rate
        from .rate_learning import record_winning_bid

        job = db.get(Job, proposal.job_id)
        if job is not None:
            rate = pick_rate(db, proposal.user_id, job)
            category = rate.skill_category if rate else "general"
            record_winning_bid(db, proposal.user_id, category, proposal.bid_amount)
    db.commit()


def record_rejection(db: Session, proposal: ProposalQueueItem, reason: str,
                     notes: str = "") -> RejectionFeedback:
    fb = RejectionFeedback(
        user_id=proposal.user_id,
        proposal_id=proposal.id, platform=proposal.platform,
        reason=reason if reason in _REASON_EFFECTS else "other", notes=notes,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
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

    Selecting a template COUNTS as a use: `uses` is incremented for every
    template returned here (they get injected as few-shot examples or shown
    as reviewer suggestions).
    """
    candidates = (
        db.query(Template)
        .filter(Template.user_id == user_id,
                Template.platform == platform)
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
    for tpl in selected:
        tpl.uses += 1
    if selected:
        db.commit()
    return selected
