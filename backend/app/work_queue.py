"""Durable generation intent; Celery is a delivery mechanism, not the record of work."""

from datetime import datetime, timedelta, timezone
import secrets
from sqlalchemy import case, or_, update
from sqlalchemy.exc import IntegrityError
from .models import GenerationWork


def ensure_generation(db, job):
    row = db.get(GenerationWork, job.id)
    if row is None:
        row = GenerationWork(job_id=job.id, user_id=job.user_id, state="pending")
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(GenerationWork, job.id)
    return row


def claim_generation(db, job):
    ensure_generation(db, job)
    now = datetime.now(timezone.utc)
    token = secrets.token_hex(16)
    changed = db.execute(
        update(GenerationWork)
        .execution_options(synchronize_session=False)
        .where(
            GenerationWork.job_id == job.id,
            GenerationWork.attempts < 3,
            or_(
                GenerationWork.state == "pending",
                (GenerationWork.state == "running")
                & (GenerationWork.lease_until < now),
            ),
        )
        .values(
            state="running",
            lease_token=token,
            lease_until=now + timedelta(minutes=15),
            attempts=GenerationWork.attempts + 1,
        )
    ).rowcount
    db.commit()
    return token if changed else None


def finish_generation(db, job_id, token, succeeded, error=""):
    db.execute(
        update(GenerationWork)
        .execution_options(synchronize_session=False)
        .where(
            GenerationWork.job_id == job_id,
            GenerationWork.lease_token == token,
        )
        .values(
            state="done" if succeeded else case((GenerationWork.attempts >= 3, "failed"), else_="pending"),
            error=error[:500],
            lease_token=None,
            lease_until=None,
        )
    )
    db.commit()


def reset_generation(db, job):
    ensure_generation(db, job)
    changed = db.execute(update(GenerationWork).where(
        GenerationWork.job_id == job.id,
        or_(GenerationWork.state != "running",
            GenerationWork.lease_until < datetime.now(timezone.utc),
            GenerationWork.lease_until.is_(None)),
    ).values(state="pending", attempts=0, error="", lease_token=None, lease_until=None)).rowcount
    db.commit()
    return bool(changed)


def expire_exhausted(db):
    """A crashed final attempt becomes inspectable failure, never an auto replay."""
    db.execute(update(GenerationWork).where(
        GenerationWork.state == "running", GenerationWork.attempts >= 3,
        or_(GenerationWork.lease_until < datetime.now(timezone.utc), GenerationWork.lease_until.is_(None)),
    ).values(state="failed", lease_token=None, lease_until=None,
             error="Final generation attempt expired; review and retry manually"))
    db.commit()
