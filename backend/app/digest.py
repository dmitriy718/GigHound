"""Digest generation and optional email delivery.

SMTP is configured via env: SMTP_HOST, SMTP_PORT (587), SMTP_USER,
SMTP_PASSWORD, DIGEST_FROM, DIGEST_TO. Without SMTP_HOST the digest is
generated but only logged/returned — nothing is sent.
"""
import logging
import os
import smtplib
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


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
        smtp.starttls()
        if os.getenv("SMTP_USER"):
            smtp.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(msg)
    return True
