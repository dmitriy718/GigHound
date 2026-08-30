from fastapi import (APIRouter, Depends, HTTPException, Query, WebSocket,
                     WebSocketDisconnect)
from sqlalchemy.orm import Session

from ..auth import (get_current_user, get_or_create_dev_user,
                    get_user_from_token)
from ..config import DEV_NOAUTH
from ..database import get_db
from ..models import AlertSettings, Job, User
from ..schemas import AlertSettingsSchema, JobOut
from ..ws_manager import alerts

router = APIRouter(tags=["alerts"])


def _get_or_create_settings(db: Session, user_id: int) -> AlertSettings:
    """Per-user settings row (singleton per tenant)."""
    settings = db.query(AlertSettings).filter(AlertSettings.user_id == user_id).first()
    if not settings:
        settings = AlertSettings(user_id=user_id)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@router.get("/api/alerts/settings", response_model=AlertSettingsSchema)
def get_settings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _get_or_create_settings(db, user.id)


@router.put("/api/alerts/settings", response_model=AlertSettingsSchema)
def update_settings(body: AlertSettingsSchema, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    settings = _get_or_create_settings(db, user.id)
    for k, v in body.model_dump().items():
        setattr(settings, k, v)
    db.commit()
    db.refresh(settings)
    return settings


def _digest_jobs(db: Session, user: User) -> tuple[AlertSettings, list[Job]]:
    from ..digest import digest_jobs_for_user

    settings = _get_or_create_settings(db, user.id)
    _, jobs = digest_jobs_for_user(db, user.id)
    return settings, jobs


@router.get("/api/alerts/digest-preview", response_model=dict)
def digest_preview(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Jobs that would appear in the next digest (per digest_mode window)."""
    _, jobs = _digest_jobs(db, user)
    return {"jobs": [JobOut.model_validate(j) for j in jobs]}


@router.post("/api/alerts/digest/send", response_model=dict)
def digest_send(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Generate the digest and email it if SMTP is configured."""
    from ..digest import send_digest_email

    settings, jobs = _digest_jobs(db, user)
    if settings.digest_mode == "off":
        raise HTTPException(400, "digest_mode is 'off'")
    sent = send_digest_email(jobs, settings.digest_mode)
    return {"jobs_in_digest": len(jobs), "emailed": sent}


@router.websocket("/ws/alerts")
async def alerts_ws(ws: WebSocket, token: str | None = Query(None),
                    db: Session = Depends(get_db)):
    """Browser WS can't set headers, so the JWT arrives as ?token= and is
    verified before accept(). GIGHOUND_DEV_NOAUTH=1 skips the check."""
    if DEV_NOAUTH:
        user = get_or_create_dev_user(db)
    else:
        user = get_user_from_token(db, token)
        if user is None:
            await ws.close(code=4401)
            return
    await alerts.connect(ws, user.id)
    try:
        while True:
            await ws.receive_text()  # client pings keep the socket alive
    except WebSocketDisconnect:
        pass
    finally:
        alerts.disconnect(ws, user.id)
