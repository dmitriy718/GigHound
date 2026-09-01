import json
import logging
import secrets

import redis
from fastapi import (APIRouter, Depends, HTTPException, Query, WebSocket,
                     WebSocketDisconnect)
from sqlalchemy.orm import Session

from ..auth import (get_current_user, get_or_create_dev_user,
                    get_user_from_token)
from ..cache import cache
from ..config import DEV_NOAUTH
from ..database import get_db
from ..models import AlertSettings, Job, User
from ..schemas import AlertSettingsSchema, JobOut
from ..ws_manager import alerts

router = APIRouter(tags=["alerts"])
log = logging.getLogger(__name__)

WS_TICKET_TTL_SECONDS = 30


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


@router.post("/api/alerts/ws-ticket", response_model=dict)
def issue_ws_ticket(user: User = Depends(get_current_user)):
    """One-time, 30s ticket for WS auth — keeps the JWT out of query strings
    (access logs). 503 when the Redis ticket store is down; the client then
    falls back to the legacy ?token= JWT path."""
    if cache._client() is None:
        raise HTTPException(503, "ws ticket store unavailable")
    ticket = secrets.token_urlsafe(32)
    cache.set_json(f"ws:ticket:{ticket}", user.id, ttl=WS_TICKET_TTL_SECONDS)
    return {"ticket": ticket}


def _consume_ws_ticket(ticket: str | None) -> int | None:
    """Look up and delete a single-use WS ticket; returns the user_id.
    None when the ticket is missing/unknown/expired or Redis is down."""
    if not ticket:
        return None
    r = cache._client()
    if r is None:
        return None
    try:
        raw = r.getdel(f"ws:ticket:{ticket}")
    except redis.RedisError as exc:
        cache._r = None
        log.warning("Redis getdel failed (%s); ticket rejected", exc)
        return None
    if not raw:
        return None
    try:
        return int(json.loads(raw))
    except (TypeError, ValueError):
        return None


@router.websocket("/ws/alerts")
async def alerts_ws(ws: WebSocket, token: str | None = Query(None),
                    ticket: str | None = Query(None),
                    db: Session = Depends(get_db)):
    """Browser WS can't set headers, so auth arrives as a one-time ?ticket=
    (from POST /api/alerts/ws-ticket), verified before accept(). The legacy
    ?token= JWT path is kept as a fallback for when the Redis ticket store
    is down. GIGHOUND_DEV_NOAUTH=1 skips the check."""
    if DEV_NOAUTH:
        user = get_or_create_dev_user(db)
    else:
        user = None
        ticket_user_id = _consume_ws_ticket(ticket)
        if ticket_user_id is not None:
            candidate = db.get(User, ticket_user_id)
            if candidate and candidate.is_active:
                user = candidate
        if user is None:
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
