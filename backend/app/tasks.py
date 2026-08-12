"""Celery app + beat schedule.

Tasks are thin async wrappers: the actual site interaction is executed by
the external stealth-browser worker pool, which polls
`GET /api/gigs/stealth-tasks` and posts results back. These tasks enqueue
work and process results — circuit-breaker gated throughout.

Run:
    celery -A app.tasks worker --loglevel=info
    celery -A app.tasks beat --loglevel=info
"""
import logging

from celery import Celery
from celery.schedules import crontab

from .config import REDIS_URL
from .database import SessionLocal
from .fiverr_monitor import enqueue_buyer_request_fetch
from .gig_analytics import enqueue_metrics_scrape

log = logging.getLogger(__name__)

celery_app = Celery("gighound", broker=REDIS_URL, backend=REDIS_URL)

celery_app.conf.beat_schedule = {
    "fiverr-buyer-request-monitor": {
        "task": "app.tasks.fiverr_buyer_request_tick",
        "schedule": 15 * 60,  # every 15 minutes
    },
    "gig-analytics-weekly": {
        "task": "app.tasks.gig_analytics_tick",
        "schedule": crontab(hour=6, minute=12, day_of_week="mon"),
    },
}
celery_app.conf.timezone = "UTC"


@celery_app.task(name="app.tasks.fiverr_buyer_request_tick")
def fiverr_buyer_request_tick() -> dict:
    """Enqueue a buyer-request fetch for the stealth worker (circuit-gated)."""
    db = SessionLocal()
    try:
        task = enqueue_buyer_request_fetch(db)
        return {"enqueued": task.id if task else None}
    finally:
        db.close()


@celery_app.task(name="app.tasks.gig_analytics_tick")
def gig_analytics_tick() -> dict:
    """Weekly: enqueue per-platform metrics scrapes (circuit-gated)."""
    db = SessionLocal()
    try:
        tasks = enqueue_metrics_scrape(db)
        return {"enqueued": [t.id for t in tasks]}
    finally:
        db.close()
