import json
import asyncio
import logging
import secrets

import redis
from fastapi import (APIRouter, Depends, HTTPException, Query, Request, WebSocket,
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
    from ..digest import send_user_digest

    settings, jobs = _digest_jobs(db, user)
    if settings.digest_mode == "off":
        raise HTTPException(400, "digest_mode is 'off'")
    sent = send_user_digest(db, user.id) > 0
    return {"jobs_in_digest": len(jobs), "emailed": sent}


@router.post("/api/alerts/ws-ticket", response_model=dict)
def issue_ws_ticket(request: Request, user: User = Depends(get_current_user)):
    """One-time, 30s ticket for WS auth — keeps the JWT out of query strings
    (access logs). 503 when the Redis ticket store is down; the client then
    falls back to the legacy ?token= JWT path."""
    if cache._client() is None:
        raise HTTPException(503, "ws ticket store unavailable")
    ticket = secrets.token_urlsafe(32)
    cache.set_json(f"ws:ticket:{ticket}", {"user_id": user.id, "token": request.headers.get("authorization", "")[7:]}, ttl=WS_TICKET_TTL_SECONDS)
    return {"ticket": ticket}


def _consume_ws_ticket(ticket: str | None) -> dict | None:
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
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
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
    auth_token = None
    if DEV_NOAUTH:
        user = get_or_create_dev_user(db)
    else:
        transaction = _consume_ws_ticket(ticket)
        if transaction:
            auth_token = transaction.get("token")
        user = get_user_from_token(db, auth_token)
        if user is None:
            db.close()
            await ws.close(code=4401)
            return
    user_id = user.id
    db.close()  # do not reserve a pooled connection for an idle WebSocket
    await alerts.connect(ws, user_id)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=15)
            except asyncio.TimeoutError:
                pass
            if not DEV_NOAUTH:
                try:
                    valid = get_user_from_token(db, auth_token) is not None
                finally:
                    db.close()
                if not valid:
                    await ws.close(code=4401)
                    break
    except WebSocketDisconnect:
        pass
    finally:
        db.close()
        alerts.disconnect(ws, user_id)
