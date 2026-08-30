"""Gig performance tracking + suggestion engine + competitor price monitoring."""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import circuit_breaker
from .models import AuditLog, CompetitorSnapshot, Gig, GigMetric, StealthTask

log = logging.getLogger(__name__)


def record_metrics(db: Session, gig: Gig, impressions: int, clicks: int,
                   orders: int, revenue: float, week: str | None = None) -> GigMetric:
    """Store a weekly snapshot and attach auto-suggestions."""
    week = week or datetime.now(timezone.utc).strftime("%G-W%V")
    metric = GigMetric(
        user_id=gig.user_id,
        gig_id=gig.id, week=week, impressions=impressions, clicks=clicks,
        orders=orders, revenue=revenue,
        suggestions=build_suggestions(impressions, clicks, orders),
    )
    db.add(metric)
    db.commit()
    db.refresh(metric)
    return metric


def build_suggestions(impressions: int, clicks: int, orders: int) -> list[dict]:
    """Rule-based tweak suggestions for underperforming gigs."""
    suggestions = []
    if impressions < 100:
        suggestions.append({
            "area": "title_keywords",
            "message": "Impressions under 100/week — rework the title with higher-volume keywords; "
                       "check competitor titles for terms you're missing.",
        })
    if impressions >= 100 and clicks / max(impressions, 1) < 0.02:
        suggestions.append({
            "area": "thumbnail",
            "message": "Click-through below 2% — refresh the thumbnail/gallery; "
                       "top gigs use high-contrast mockups with outcome text.",
        })
    if clicks >= 20 and orders / max(clicks, 1) < 0.05:
        suggestions.append({
            "area": "pricing_or_description",
            "message": "Conversion below 5% — consider a lower Basic tier price or a sharper "
                       "description hook; compare against competitor pricing.",
        })
    return suggestions


def competitor_price_analysis(snapshot_gigs: list[dict], my_price: float | None) -> list[str]:
    """Insights from a competitor snapshot vs. my Basic price."""
    prices = [g.get("price") for g in snapshot_gigs if g.get("price")]
    insights = []
    if not prices:
        return insights
    avg = sum(prices) / len(prices)
    low, high = min(prices), max(prices)
    insights.append(f"Top {len(prices)} gigs price between ${low:g} and ${high:g} (avg ${avg:.0f}).")
    if my_price:
        if my_price > avg * 1.2:
            insights.append(f"You're priced {((my_price/avg)-1)*100:.0f}% above market "
                            f"(${my_price:g} vs avg ${avg:.0f}) — consider ${avg*0.95:.0f} to compete.")
        elif my_price < avg * 0.8:
            insights.append(f"You're priced {((1-my_price/avg))*100:.0f}% below market — "
                            f"room to raise toward ${avg*0.9:.0f} without losing volume.")
    return insights


def store_competitor_snapshot(db: Session, user_id: int, platform: str, category: str,
                              gigs: list[dict], my_price: float | None = None) -> CompetitorSnapshot:
    snap = CompetitorSnapshot(
        user_id=user_id,
        platform=platform, category=category, gigs=gigs,
        insights=competitor_price_analysis(gigs, my_price),
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def enqueue_metrics_scrape(db: Session, user_id: int) -> list[StealthTask]:
    """Weekly task: one stealth scrape per platform with active gigs."""
    platforms = [p for (p,) in db.query(Gig.platform).filter(Gig.user_id == user_id).distinct().all()]
    tasks = []
    for platform in platforms:
        allowed, reason = circuit_breaker.check(platform)
        task = StealthTask(
            user_id=user_id,
            platform=platform, task_type="gig_scrape_metrics",
            payload={"gigs": [{"id": g.id, "url": g.url, "title": g.title}
                              for g in db.query(Gig).filter(
                                  Gig.user_id == user_id, Gig.platform == platform).all()]},
            status="pending" if allowed else "skipped_circuit_open",
            result={} if allowed else {"reason": reason},
        )
        db.add(task)
        if allowed:
            tasks.append(task)
        else:
            log.warning("metrics scrape skipped for %s: %s", platform, reason)
    db.commit()
    for t in tasks:
        db.refresh(t)
    db.add(AuditLog(user_id=user_id, action_type="gig_metrics_scrape_queued",
                    detail={"platforms": [t.platform for t in tasks]}))
    db.commit()
    return tasks
