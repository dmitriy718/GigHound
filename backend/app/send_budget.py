"""Durable, idempotent daily reservations for external write attempts."""

import os
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from .models import AuthTransaction, User


def submission_cap(platform):
    raw = os.getenv(
        "GIGHOUND_DAILY_SUBMIT_CAP_" + platform.upper(),
        "10" if platform == "fiverr" else "0",
    )
    try:
        cap = int(raw)
    except ValueError:
        raise HTTPException(503, "submission budget configuration is invalid") from None
    return cap


def reserve_send(db, task):
    if task.task_type not in ("submit_upwork_proposal", "submit_fiverr_offer"):
        return
    cap = submission_cap(task.platform)
    if cap <= 0:
        return
    owner = db.get(User, task.user_id)
    db.refresh(owner, with_for_update=True)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    key = f"send:{task.id}:{day}"
    if db.get(AuthTransaction, key):
        return
    used = (
        db.query(AuthTransaction)
        .filter(
            AuthTransaction.user_id == task.user_id,
            AuthTransaction.kind == "send_attempt",
            AuthTransaction.payload["platform"].as_string() == task.platform,
            AuthTransaction.payload["day"].as_string() == day,
        )
        .count()
    )
    if used >= cap:
        raise HTTPException(409, "daily external-write attempt budget is exhausted")
    db.add(
        AuthTransaction(
            id=key,
            user_id=task.user_id,
            kind="send_attempt",
            payload={"platform": task.platform, "day": day, "task_id": task.id},
            expires_at=now + timedelta(days=2),
        )
    )
    db.flush()


def reserve_circuit_trials(db, task):
    """Bind half-open permission to this exact task until its claim expires."""
    from . import circuit_breaker
    from sqlalchemy.exc import IntegrityError

    for scope in (None, task.user_id):
        current = circuit_breaker.get_state(task.platform, scope, db=db)
        state = current.get("state")
        if state == "open":
            raise HTTPException(409, "platform automation is paused")
        if state != "half_open":
            continue
        key = f"circuittrial:{task.platform}:{scope}"
        row = (
            db.query(AuthTransaction)
            .filter_by(id=key)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        now = datetime.now(timezone.utc)
        if row is not None:
            expiry = (
                row.expires_at.replace(tzinfo=timezone.utc)
                if row.expires_at.tzinfo is None
                else row.expires_at
            )
            if (expiry > now and row.payload.get("task_id") != task.id
                    and row.payload.get("circuit_revision") == current["revision"]):
                raise HTTPException(409, "another task owns the circuit trial")
        else:
            row = AuthTransaction(id=key, user_id=task.user_id, kind="circuit_trial")
            db.add(row)
        row.payload = {"task_id": task.id, "scope": scope, "platform": task.platform,
                       "circuit_revision": current["revision"]}
        row.expires_at = now + timedelta(minutes=15)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                409, "another task acquired the circuit trial"
            ) from None


def finish_circuit_trials(db, task, success):
    """Only the owning task may resolve a half-open trial."""
    from . import circuit_breaker

    for scope in (None, task.user_id):
        row = (
            db.query(AuthTransaction)
            .filter_by(id=f"circuittrial:{task.platform}:{scope}")
            .with_for_update()
            .one_or_none()
        )
        if row is not None and row.payload.get("task_id") == task.id:
            current = circuit_breaker.get_state(task.platform, scope, db=db)
            if (current.get("state") == "half_open"
                    and row.payload.get("circuit_revision") == current["revision"]):
                circuit_breaker.transition(
                    task.platform,
                    "closed" if success else "open",
                    "owning trial completed",
                    scope, db=db,
                )
            db.delete(row)
