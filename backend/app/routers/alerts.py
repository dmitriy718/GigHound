from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AlertSettings, Job
from ..schemas import AlertSettingsSchema, JobOut
from ..ws_manager import alerts

router = APIRouter(tags=["alerts"])


def _get_or_create_settings(db: Session) -> AlertSettings:
    settings = db.query(AlertSettings).first()
    if not settings:
        settings = AlertSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@router.get("/api/alerts/settings", response_model=AlertSettingsSchema)
def get_settings(db: Session = Depends(get_db)):
    return _get_or_create_settings(db)


@router.put("/api/alerts/settings", response_model=AlertSettingsSchema)
def update_settings(body: AlertSettingsSchema, db: Session = Depends(get_db)):
    settings = _get_or_create_settings(db)
    for k, v in body.model_dump().items():
        setattr(settings, k, v)
    db.commit()
    db.refresh(settings)
    return settings


@router.get("/api/alerts/digest-preview", response_model=dict)
def digest_preview(db: Session = Depends(get_db)):
    """Jobs that would appear in the next digest (per digest_mode window)."""
    settings = _get_or_create_settings(db)
    hours = {"hourly": 1, "daily": 24}.get(settings.digest_mode, 24)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    jobs = (
        db.query(Job)
        .filter(
            Job.status.in_(["new", "notified"]),
            Job.quality_score >= settings.min_score_alert,
            Job.fetched_at >= since,
        )
        .order_by(Job.quality_score.desc())
        .limit(50)
        .all()
    )
    return {"jobs": [JobOut.model_validate(j) for j in jobs]}


@router.post("/api/alerts/digest/send", response_model=dict)
def digest_send(db: Session = Depends(get_db)):
    """Generate the digest and email it if SMTP is configured."""
    from ..digest import send_digest_email

    settings = _get_or_create_settings(db)
    if settings.digest_mode == "off":
        raise HTTPException(400, "digest_mode is 'off'")
    hours = {"hourly": 1, "daily": 24}[settings.digest_mode]
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    jobs = (
        db.query(Job)
        .filter(
            Job.status.in_(["new", "notified"]),
            Job.quality_score >= settings.min_score_alert,
            Job.fetched_at >= since,
        )
        .order_by(Job.quality_score.desc())
        .limit(50)
        .all()
    )
    sent = send_digest_email(jobs, settings.digest_mode)
    return {"jobs_in_digest": len(jobs), "emailed": sent}


@router.websocket("/ws/alerts")
async def alerts_ws(ws: WebSocket):
    await alerts.connect(ws)
    try:
        while True:
            await ws.receive_text()  # client pings keep the socket alive
    except WebSocketDisconnect:
        pass
    finally:
        alerts.disconnect(ws)
