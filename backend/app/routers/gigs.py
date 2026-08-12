"""Gig management endpoints: templates, creation triggers, analytics,
competitor intel, buyer-request inbox, and stealth-task handoff."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import circuit_breaker, fiverr_monitor, gig_templates as gt
from ..database import get_db
from ..gig_analytics import (enqueue_metrics_scrape, record_metrics,
                             store_competitor_snapshot)
from ..models import Gig, GigMetric, GigTemplate, CompetitorSnapshot, StealthTask
from ..schemas import (CompetitorSnapshotOut, GigMetricIn, GigMetricOut,
                       GigOut, GigTemplateIn, GigTemplateOut)

router = APIRouter(prefix="/api/gigs", tags=["gigs"])


# --- taxonomy & SEO helpers ---

@router.get("/taxonomy/fiverr", response_model=dict)
def fiverr_taxonomy():
    return {"categories": gt.FIVERR_CATEGORIES,
            "note": "seed dataset — refresh from Fiverr seller dashboard when it drifts"}


@router.post("/seo-title-score", response_model=dict)
def seo_title_score(body: dict):
    return gt.seo_title_score(body.get("title", ""), body.get("keywords") or [])


@router.post("/faqs/generate", response_model=dict)
async def generate_faqs(body: dict):
    faqs = await gt.generate_faqs(body.get("gig_type", ""), body.get("title", ""),
                                  int(body.get("count", 4)))
    return {"faqs": faqs}


# --- template CRUD ---

@router.get("/templates", response_model=list[GigTemplateOut])
def list_templates(platform: str | None = None, db: Session = Depends(get_db)):
    q = db.query(GigTemplate)
    if platform:
        q = q.filter(GigTemplate.platform == platform)
    return q.all()


@router.post("/templates", response_model=GigTemplateOut, status_code=201)
def create_template(body: GigTemplateIn, db: Session = Depends(get_db)):
    tpl, problems = gt.create_template(db, body.platform, body.name,
                                       body.template_json, body.auto_publish)
    if problems:
        raise HTTPException(422, {"validation": problems})
    return tpl


@router.put("/templates/{tpl_id}", response_model=GigTemplateOut)
def update_template(tpl_id: int, body: GigTemplateIn, db: Session = Depends(get_db)):
    tpl = db.get(GigTemplate, tpl_id)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    validator = (gt.validate_fiverr_template if body.platform == "fiverr"
                 else gt.validate_upwork_catalog_template if body.platform == "upwork"
                 else lambda d: [])
    problems = validator(body.template_json)
    if problems:
        raise HTTPException(422, {"validation": problems})
    tpl.platform, tpl.name, tpl.template_json = body.platform, body.name, body.template_json
    tpl.auto_publish = body.auto_publish
    db.commit()
    db.refresh(tpl)
    return tpl


@router.delete("/templates/{tpl_id}", status_code=204)
def delete_template(tpl_id: int, db: Session = Depends(get_db)):
    tpl = db.get(GigTemplate, tpl_id)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    db.delete(tpl)
    db.commit()


@router.post("/templates/{tpl_id}/toggle", response_model=GigTemplateOut)
def toggle_template(tpl_id: int, db: Session = Depends(get_db)):
    tpl = db.get(GigTemplate, tpl_id)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    tpl.is_active = not tpl.is_active
    db.commit()
    db.refresh(tpl)
    return tpl


# --- gig creation (queues stealth task; DRAFT only for Fiverr) ---

@router.post("/templates/{tpl_id}/create-gig", response_model=dict)
def create_gig_from_template(tpl_id: int, db: Session = Depends(get_db)):
    tpl = db.get(GigTemplate, tpl_id)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    if tpl.platform == "fiverr":
        task, err = fiverr_monitor.queue_gig_creation(db, tpl)
    elif tpl.platform == "upwork":
        task, err = fiverr_monitor.queue_upwork_catalog_upsert(db, tpl)
    else:
        raise HTTPException(400, f"gig creation not supported for '{tpl.platform}'")
    if err:
        raise HTTPException(429, err)
    return {"stealth_task_id": task.id, "status": task.status,
            "note": "gig will be saved as DRAFT — never auto-published"
                    if tpl.platform == "fiverr" else
                    f"auto_publish={'on' if tpl.auto_publish else 'off (draft for review)'}"}


# --- gigs & metrics ---

@router.get("", response_model=list[GigOut])
def list_gigs(platform: str | None = None, status: str | None = None,
              db: Session = Depends(get_db)):
    q = db.query(Gig)
    if platform:
        q = q.filter(Gig.platform == platform)
    if status:
        q = q.filter(Gig.status == status)
    return q.all()


@router.post("", response_model=GigOut, status_code=201)
def register_gig(body: dict, db: Session = Depends(get_db)):
    """Register an externally-created gig for tracking."""
    gig = Gig(
        platform=body["platform"], title=body.get("title", ""),
        external_id=body.get("external_id", ""), url=body.get("url", ""),
        status=body.get("status", "draft"), price_min=body.get("price_min"),
        template_id=body.get("template_id"),
    )
    db.add(gig)
    db.commit()
    db.refresh(gig)
    return gig


@router.get("/metrics", response_model=list[GigMetricOut])
def list_metrics(gig_id: int, db: Session = Depends(get_db)):
    return (db.query(GigMetric).filter(GigMetric.gig_id == gig_id)
            .order_by(GigMetric.week).all())


@router.post("/metrics", response_model=GigMetricOut, status_code=201)
def ingest_metrics(body: GigMetricIn, db: Session = Depends(get_db)):
    """Stealth worker posts weekly scrape results here."""
    gig = db.get(Gig, body.gig_id)
    if not gig:
        raise HTTPException(404, "gig not found")
    return record_metrics(db, gig, body.impressions, body.clicks,
                          body.orders, body.revenue, body.week)


@router.post("/metrics/scrape", response_model=dict)
def trigger_metrics_scrape(db: Session = Depends(get_db)):
    tasks = enqueue_metrics_scrape(db)
    return {"queued_tasks": [t.id for t in tasks]}


# --- competitor intel ---

@router.get("/competitors", response_model=list[CompetitorSnapshotOut])
def list_competitor_snapshots(platform: str, category: str | None = None,
                              db: Session = Depends(get_db)):
    q = db.query(CompetitorSnapshot).filter(CompetitorSnapshot.platform == platform)
    if category:
        q = q.filter(CompetitorSnapshot.category == category)
    return q.order_by(CompetitorSnapshot.created_at.desc()).limit(20).all()


@router.post("/competitors", response_model=CompetitorSnapshotOut, status_code=201)
def ingest_competitor_snapshot(body: dict, db: Session = Depends(get_db)):
    """Stealth worker posts top-10 category scrape results here."""
    return store_competitor_snapshot(db, body["platform"], body["category"],
                                     body.get("gigs", []), body.get("my_price"))


# --- buyer request inbox ---

@router.get("/buyer-requests", response_model=dict)
def buyer_request_inbox(db: Session = Depends(get_db)):
    from ..models import ProposalQueueItem
    items = (db.query(ProposalQueueItem)
             .filter(ProposalQueueItem.request_type == "buyer_request")
             .order_by(ProposalQueueItem.created_at.desc()).limit(100).all())
    return {"offers_remaining_today": fiverr_monitor.offers_remaining_today(),
            "daily_limit": fiverr_monitor.FIVERR_DAILY_OFFER_LIMIT,
            "count": len(items)}


@router.post("/buyer-requests/process", response_model=dict)
def process_buyer_requests(body: dict, db: Session = Depends(get_db)):
    """Stealth worker posts scraped buyer requests here for filtering + offers."""
    return fiverr_monitor.process_buyer_requests(db, body.get("requests", []))


# --- stealth task handoff (browser worker polling) ---

@router.get("/stealth-tasks", response_model=list[dict])
def poll_stealth_tasks(platform: str | None = None, status: str = "pending",
                       db: Session = Depends(get_db)):
    q = db.query(StealthTask).filter(StealthTask.status == status)
    if platform:
        q = q.filter(StealthTask.platform == platform)
    return [{"id": t.id, "platform": t.platform, "task_type": t.task_type,
             "payload": t.payload, "status": t.status, "created_at": t.created_at}
            for t in q.order_by(StealthTask.created_at).limit(50).all()]


@router.post("/stealth-tasks/{task_id}/complete", response_model=dict)
def complete_stealth_task(task_id: int, body: dict, db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    task = db.get(StealthTask, task_id)
    if not task:
        raise HTTPException(404, "stealth task not found")
    task.status = "done" if body.get("success", True) else "failed"
    task.result = body.get("result", {})
    task.completed_at = datetime.now(timezone.utc)
    if not body.get("success", True):
        # repeated failures trip the circuit breaker
        recent_failures = (db.query(StealthTask)
                           .filter(StealthTask.platform == task.platform,
                                   StealthTask.status == "failed")
                           .count())
        if recent_failures >= 3:
            circuit_breaker.open_circuit(task.platform, "repeated stealth task failures")
    db.commit()
    return {"id": task.id, "status": task.status}


# --- circuit breaker controls ---

@router.get("/circuit/{platform}", response_model=dict)
def circuit_state(platform: str):
    return circuit_breaker.get_state(platform)


@router.post("/circuit/{platform}", response_model=dict)
def set_circuit(platform: str, body: dict):
    state = body.get("state")
    if state == "open":
        circuit_breaker.open_circuit(platform, body.get("reason", "manual"))
    elif state == "closed":
        circuit_breaker.close_circuit(platform, body.get("reason", "manual reset"))
    else:
        raise HTTPException(400, "state must be 'open' or 'closed'")
    return circuit_breaker.get_state(platform)
