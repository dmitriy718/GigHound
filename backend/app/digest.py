"""Digest generation and optional email delivery.

SMTP is configured via env: SMTP_HOST, SMTP_PORT (587), SMTP_USER,
SMTP_PASSWORD, DIGEST_FROM, DIGEST_TO, SMTP_TLS (default true; set false for
plain local relays without STARTTLS). Without SMTP_HOST the digest is
generated but only logged/returned — nothing is sent.
"""
import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from .models import AlertSettings, Job, User

log = logging.getLogger(__name__)


def digest_jobs_for_user(db: Session, user_id: int) -> tuple[AlertSettings | None, list]:
    """Settings + the jobs that would appear in the user's next digest
    (per digest_mode window). (None, []) when the user has no settings row."""
    settings = db.query(AlertSettings).filter(AlertSettings.user_id == user_id).first()
    if settings is None:
        return None, []
    hours = {"hourly": 1, "daily": 24}.get(settings.digest_mode, 24)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    jobs = (
        db.query(Job)
        .filter(
            Job.user_id == user_id,
            Job.status.in_(["new", "notified"]),
            Job.quality_score >= settings.min_score_alert,
            Job.fetched_at >= since,
        )
        .order_by(Job.quality_score.desc())
        .limit(50)
        .all()
    )
    return settings, jobs


def due_digest_user_ids(db: Session, now: datetime | None = None) -> list[int]:
    """User ids whose digest mode is due right now (hourly always; daily only
    in the 07:00 UTC hour). The tick dispatcher fans out one task per id."""
    now = now or datetime.now(timezone.utc)
    rows = (db.query(AlertSettings)
            .join(User, AlertSettings.user_id == User.id)
            .filter(AlertSettings.digest_mode.in_(["hourly", "daily"]),
                    User.is_active.is_(True))  # deactivated tenants get no digest
            .all())
    return [s.user_id for s in rows
            if not (s.digest_mode == "daily" and now.hour != 7)]


def send_user_digest(db: Session, user_id: int) -> int:
    """Send one user's digest. Returns the job count actually emailed (0 when
    there is nothing to report, the user has no settings row, or the send
    was skipped — e.g. SMTP not configured — so the beat-reported `sent`
    count never claims digests that were only logged)."""
    settings, jobs = digest_jobs_for_user(db, user_id)
    if settings is None or not jobs:
        return 0
    if not send_digest_email(jobs, settings.digest_mode):
        log.warning("digest_mode active but SMTP not configured; digest not emailed")
        return 0
    return len(jobs)


def render_digest(jobs: list, mode: str) -> str:
    lines = [f"GigHound — {mode} digest ({len(jobs)} jobs)", ""]
    for j in jobs:
        budget = ""
        if j.budget_usd_min or j.budget_usd_max:
            budget = f" | ${j.budget_usd_min or '?'}-${j.budget_usd_max or '?'}"
        lines.append(f"[{j.quality_score:.0f}] {j.title} ({j.platform}){budget}")
        lines.append(f"     {j.url}")
    return "\n".join(lines)


def send_digest_email(jobs: list, mode: str) -> bool:
    """Send the digest via SMTP if configured. Returns True when sent."""
    host = os.getenv("SMTP_HOST")
    body = render_digest(jobs, mode)
    if not host:
        log.info("SMTP_HOST not set — digest not emailed.\n%s", body)
        return False
    msg = MIMEText(body)
    msg["Subject"] = f"GigHound {mode} digest — {len(jobs)} jobs"
    msg["From"] = os.getenv("DIGEST_FROM", "gighound@localhost")
    msg["To"] = os.getenv("DIGEST_TO", "")
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as smtp:
        # STARTTLS by default; SMTP_TLS=false for plain local relays
        if os.getenv("SMTP_TLS", "true").strip().lower() not in ("0", "false", "no"):
            smtp.starttls()
        if os.getenv("SMTP_USER"):
            smtp.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(msg)
    return True
