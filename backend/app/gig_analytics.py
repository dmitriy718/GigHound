"""Gig performance tracking + suggestion engine + competitor price monitoring."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import circuit_breaker
from .models import AuditLog, CompetitorSnapshot, Gig, GigMetric, StealthTask

# canonical set lives in app.platforms (mirror of worker/platforms.py)
from .platforms import WORKER_PLATFORMS
from .stealth import SCRAPE_GIG_METRICS

log = logging.getLogger(__name__)


def record_metrics(
    db: Session,
    gig: Gig,
    impressions: int,
    clicks: int,
    orders: int,
    revenue: float,
    week: str | None = None,
) -> GigMetric:
    """Store a weekly snapshot and attach auto-suggestions."""
    week = week or datetime.now(timezone.utc).strftime("%G-W%V")
    db.refresh(gig, with_for_update=True)
    metric = (
        db.query(GigMetric)
        .filter_by(user_id=gig.user_id, gig_id=gig.id, week=week)
        .order_by(GigMetric.id.desc())
        .first()
    )
    if metric is None:
        metric = GigMetric(user_id=gig.user_id, gig_id=gig.id, week=week)
    metric.impressions, metric.clicks, metric.orders, metric.revenue = (
        impressions,
        clicks,
        orders,
        revenue,
    )
    metric.suggestions = build_suggestions(impressions, clicks, orders)
    db.add(metric)
    db.commit()
    db.refresh(metric)
    return metric


def build_suggestions(impressions: int, clicks: int, orders: int) -> list[dict]:
    """Rule-based tweak suggestions for underperforming gigs."""
    suggestions = []
    if impressions is not None and impressions < 100:
        suggestions.append(
            {
                "area": "title_keywords",
                "message": "Impressions under 100/week — rework the title with higher-volume keywords; "
                "check competitor titles for terms you're missing.",
            }
        )
    if (
        impressions is not None
        and clicks is not None
        and impressions >= 100
        and clicks / max(impressions, 1) < 0.02
    ):
        suggestions.append(
            {
                "area": "thumbnail",
                "message": "Click-through below 2% — refresh the thumbnail/gallery; "
                "top gigs use high-contrast mockups with outcome text.",
            }
        )
    if (
        clicks is not None
        and orders is not None
        and clicks >= 20
        and orders / max(clicks, 1) < 0.05
    ):
        suggestions.append(
            {
                "area": "pricing_or_description",
                "message": "Conversion below 5% — consider a lower Basic tier price or a sharper "
                "description hook; compare against competitor pricing.",
            }
        )
    return suggestions


def competitor_price_analysis(
    snapshot_gigs: list[dict], my_price: float | None
) -> list[str]:
    """Insights from a competitor snapshot vs. my Basic price."""
    prices = [g.get("price") for g in snapshot_gigs if g.get("price")]
    insights = []
    if not prices:
        return insights
    avg = sum(prices) / len(prices)
    low, high = min(prices), max(prices)
    insights.append(
        f"Top {len(prices)} gigs price between ${low:g} and ${high:g} (avg ${avg:.0f})."
    )
    if my_price:
        if my_price > avg * 1.2:
            insights.append(
                f"You're priced {((my_price/avg)-1)*100:.0f}% above market "
                f"(${my_price:g} vs avg ${avg:.0f}) — consider ${avg*0.95:.0f} to compete."
            )
        elif my_price < avg * 0.8:
            insights.append(
                f"You're priced {((1-my_price/avg))*100:.0f}% below market — "
                f"room to raise toward ${avg*0.9:.0f} without losing volume."
            )
    return insights


def store_competitor_snapshot(
    db: Session,
    user_id: int,
    platform: str,
    category: str,
    gigs: list[dict],
    my_price: float | None = None,
    *, commit: bool = True,
) -> CompetitorSnapshot:
    snap = CompetitorSnapshot(
        user_id=user_id,
        platform=platform,
        category=category,
        gigs=gigs,
        insights=competitor_price_analysis(gigs, my_price),
    )
    db.add(snap)
    if commit:
        db.commit()
        db.refresh(snap)
    else:
        db.flush()
    return snap


def enqueue_metrics_scrape(db: Session, user_id: int, *, report: dict | None = None) -> list[StealthTask]:
    """Only explicitly assigned listings; one task per current seller identity."""
    from .models import PlatformAccount
    accounts = {a.id: a for a in db.query(PlatformAccount).filter(
        PlatformAccount.user_id == user_id, PlatformAccount.enabled.is_(True),
        PlatformAccount.mode.in_(["stealth", "hybrid"])).all()}
    groups, skipped = {}, []
    for gig in db.query(Gig).filter(Gig.user_id == user_id, Gig.status.in_(["draft", "active"])).all():
        account = accounts.get(gig.account_id)
        if (gig.platform not in WORKER_PLATFORMS or not account or
            account.platform != gig.platform or account.identity_epoch != gig.account_epoch):
            skipped.append(gig.id)
            continue
        groups.setdefault((gig.platform, account.id, account.identity_epoch), []).append(gig)
    tasks = []
    for (platform, account_id, epoch), gigs in groups.items():
        allowed, reason = circuit_breaker.check(platform, user_id, db=db)
        if not allowed:
            skipped.extend(g.id for g in gigs)
            continue
        task = StealthTask(user_id=user_id, platform=platform, task_type=SCRAPE_GIG_METRICS,
            payload={"account_id": account_id, "account_epoch": epoch,
                     "gigs": [{"id": g.id, "url": g.url, "title": g.title,
                               "account_binding_version": g.account_binding_version} for g in gigs]})
        db.add(task)
        tasks.append(task)
    db.add(AuditLog(user_id=user_id, action_type="gig_metrics_scrape_queued",
                   detail={"platforms": [t.platform for t in tasks], "skipped_gig_ids": skipped}))
    db.commit()
    if report is not None:
        report["skipped_gig_ids"] = skipped
    return tasks
