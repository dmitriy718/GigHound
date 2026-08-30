"""Learning-loop analytics (Phase 2.6): proposal funnel per platform,
template win rates, bid-band performance, and rejection-reason breakdown —
plus the weekly trend (submitted/replied/hired per ISO week).

All aggregation runs in SQL (GROUP BY / conditional sums) — no load-all-
then-aggregate in Python. Everything is tenant-scoped through the current
user. win_rate is a 0-100 float everywhere (null when no outcomes yet).
"""
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import ProposalQueueItem, Template, User

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

_ACTIVE_STATUSES = ("pending_review", "approved", "submitted", "queued_for_browser")
_APPROVED_STATUSES = ("approved", "submitted", "queued_for_browser")
_SUBMITTED_STATUSES = ("submitted", "queued_for_browser")

# bid_amount bands (USD): (label, lower, upper)
_BID_BANDS = [("<100", None, 100), ("100-500", 100, 500),
              ("500-1000", 500, 1000), ("1000+", 1000, None)]


def _win_rate(wins: int, losses: int) -> float | None:
    """wins/(wins+losses) as a 0-100 float; null when there are no outcomes."""
    total = wins + losses
    return round(100 * wins / total, 1) if total else None


def _count_if(cond):
    """Conditional count as an integer SUM(CASE ...) — portable (SQLite/PG)."""
    return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)


def _funnel_aggregates():
    """The funnel's conditional-count columns (shared by global/per-platform)."""
    return [
        _count_if(ProposalQueueItem.status.in_(_ACTIVE_STATUSES)).label("queued"),
        _count_if(ProposalQueueItem.status.in_(_APPROVED_STATUSES)).label("approved"),
        _count_if(ProposalQueueItem.status.in_(_SUBMITTED_STATUSES)).label("submitted"),
        _count_if(ProposalQueueItem.client_replied_at.isnot(None)).label("replied"),
        _count_if(ProposalQueueItem.outcome == "hired").label("hired"),
        _count_if(ProposalQueueItem.outcome == "rejected").label("rejected"),
        _count_if(ProposalQueueItem.outcome == "ghosted").label("ghosted"),
    ]


def _funnel_dict(row) -> dict:
    return {k: int(getattr(row, k) or 0)
            for k in ("queued", "approved", "submitted", "replied",
                      "hired", "rejected", "ghosted")}


@router.get("/funnel", response_model=dict)
def analytics_funnel(db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    base = db.query(*_funnel_aggregates()).filter(ProposalQueueItem.user_id == user.id)

    funnel = _funnel_dict(base.one())

    by_platform = []
    for row in base.group_by(ProposalQueueItem.platform).add_columns(
            ProposalQueueItem.platform.label("platform")).all():
        counts = _funnel_dict(row)
        by_platform.append({
            "platform": row.platform,
            "queued": counts["queued"],
            "approved": counts["approved"],
            "submitted": counts["submitted"],
            "replied": counts["replied"],
            "hired": counts["hired"],
            "win_rate": _win_rate(counts["hired"],
                                  counts["rejected"] + counts["ghosted"]),
        })
    by_platform.sort(key=lambda r: r["platform"])

    templates = (db.query(Template)
                 .filter(Template.user_id == user.id)
                 .order_by(Template.id)
                 .all())
    by_template = [{
        "template_id": t.id,
        "title": t.title,
        "platform": t.platform,
        "uses": t.uses,
        "wins": t.wins,
        "losses": t.losses,
        "win_rate": _win_rate(t.wins, t.losses),
    } for t in templates]

    band_expr = case(
        (ProposalQueueItem.bid_amount < 100, "<100"),
        (ProposalQueueItem.bid_amount < 500, "100-500"),
        (ProposalQueueItem.bid_amount < 1000, "500-1000"),
        else_="1000+",
    )
    band_rows = (db.query(band_expr.label("band"),
                          func.count().label("submitted"),
                          _count_if(ProposalQueueItem.outcome == "hired").label("hired"),
                          _count_if(ProposalQueueItem.outcome.in_(("rejected", "ghosted"))).label("losses"))
                 .filter(ProposalQueueItem.user_id == user.id,
                         ProposalQueueItem.bid_amount.isnot(None),
                         (ProposalQueueItem.status.in_(_SUBMITTED_STATUSES))
                         | (ProposalQueueItem.outcome != "pending"))
                 .group_by(band_expr)
                 .all())
    band_stats = {r.band: (int(r.submitted), int(r.hired or 0), int(r.losses or 0))
                  for r in band_rows}
    by_bid_band = []
    for label, _low, _high in _BID_BANDS:
        submitted, hired, losses = band_stats.get(label, (0, 0, 0))
        by_bid_band.append({"band": label, "submitted": submitted,
                            "hired": hired, "win_rate": _win_rate(hired, losses)})

    reason_rows = (db.query(ProposalQueueItem.rejection_reason.label("reason"),
                            func.count().label("count"))
                   .filter(ProposalQueueItem.user_id == user.id,
                           ProposalQueueItem.rejection_reason.isnot(None),
                           ProposalQueueItem.rejection_reason != "")
                   .group_by(ProposalQueueItem.rejection_reason)
                   .order_by(ProposalQueueItem.rejection_reason)
                   .all())

    return {
        "funnel": funnel,
        "by_platform": by_platform,
        "by_template": by_template,
        "by_bid_band": by_bid_band,
        "rejection_reasons": [{"reason": r.reason, "count": int(r.count)}
                              for r in reason_rows],
    }


# ---------------- weekly trend ----------------

def _as_date(value) -> date | None:
    """func.date() returns a date on Postgres, a 'YYYY-MM-DD' str on SQLite."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _day_counts(rows) -> dict[str, tuple[int, ...]]:
    """{(iso_week_label): counts} from (day, *counts) rows."""
    out: dict[str, tuple[int, ...]] = {}
    for row in rows:
        day = _as_date(row[0])
        if day is None:
            continue
        label = day.strftime("%G-W%V")
        prev = out.get(label) or (0,) * (len(row) - 1)
        out[label] = tuple(p + int(c or 0) for p, c in zip(prev, row[1:]))
    return out


@router.get("/trend", response_model=dict)
def analytics_trend(weeks: int = Query(8, ge=1, le=26),
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Per-ISO-week funnel trend, oldest first.

    Aggregation is by day in SQL (GROUP BY func.date(...)), bucketed into
    ISO weeks in Python — portable across SQLite/Postgres. Timestamps:
    submitted = coalesce(reviewed_at, created_at) (reviewed_at is the
    submission proxy — set at approval, the last step before submit);
    replied = client_replied_at; hired/losses = the outcome_recorded_at
    stamp written by record_outcome (legacy rows without the stamp fall
    back to created_at).
    """
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.isoweekday() - 1)
    start = monday - timedelta(days=7 * (weeks - 1))
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    labels = [(start + timedelta(days=7 * i)).strftime("%G-W%V")
              for i in range(weeks)]

    submitted_ts = func.coalesce(ProposalQueueItem.reviewed_at,
                                 ProposalQueueItem.created_at)
    submitted_rows = (db.query(func.date(submitted_ts), func.count())
                      .filter(ProposalQueueItem.user_id == user.id,
                              ProposalQueueItem.status.in_(_SUBMITTED_STATUSES),
                              submitted_ts >= start_dt)
                      .group_by(func.date(submitted_ts))
                      .all())
    replied_rows = (db.query(func.date(ProposalQueueItem.client_replied_at),
                             func.count())
                    .filter(ProposalQueueItem.user_id == user.id,
                            ProposalQueueItem.client_replied_at.isnot(None),
                            ProposalQueueItem.client_replied_at >= start_dt)
                    .group_by(func.date(ProposalQueueItem.client_replied_at))
                    .all())
    outcome_stamp = ProposalQueueItem.submission_result[
        "outcome_recorded_at"].as_string()
    outcome_day = func.coalesce(func.date(outcome_stamp),
                                func.date(ProposalQueueItem.created_at))
    outcome_rows = (db.query(outcome_day,
                             _count_if(ProposalQueueItem.outcome == "hired"),
                             _count_if(ProposalQueueItem.outcome.in_(
                                 ("rejected", "ghosted"))))
                    .filter(ProposalQueueItem.user_id == user.id,
                            ProposalQueueItem.outcome.in_(
                                ("hired", "rejected", "ghosted")))
                    .group_by(outcome_day)
                    .all())

    submitted = _day_counts(submitted_rows)
    replied = _day_counts(replied_rows)
    outcomes = _day_counts(outcome_rows)

    out = []
    for label in labels:
        hired, losses = outcomes.get(label, (0, 0))
        out.append({
            "week": label,
            "submitted": submitted.get(label, (0,))[0],
            "replied": replied.get(label, (0,))[0],
            "hired": hired,
            "win_rate": _win_rate(hired, losses),
        })
    return {"weeks": out}
