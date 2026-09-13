"""Material approval snapshot, shared by API review and dispatch checks."""

import hashlib
import json
from sqlalchemy import select
from .models import Job, PlatformAccount, AdapterCredential


def snapshot(connection, item):
    job = (
        connection.execute(select(Job.__table__).where(Job.id == item.job_id))
        .mappings()
        .first()
    )
    if job is None:
        return {}
    selected = item.platform_account_id
    # Preserve legacy approved identity until an explicit return to review.
    if selected is None and item.status != "pending_review":
        selected = (item.approved_snapshot or {}).get("account_id")
    accounts = (
        connection.execute(
            select(PlatformAccount.__table__)
            .where(
                PlatformAccount.user_id == item.user_id,
                PlatformAccount.platform == item.platform,
                PlatformAccount.enabled.is_(True),
                PlatformAccount.mode != "disabled",
                *([PlatformAccount.id == selected] if selected is not None else []),
            )
            .order_by(PlatformAccount.id)
            .limit(2)
        )
        .mappings()
        .all()
    )
    account = accounts[0] if len(accounts) == 1 else None
    config = (
        {
            "id": account["id"],
            "principal": account["principal"],
            "mode": account["mode"],
            "settings": account["settings"],
            "credential_ref": account["credential_ref"],
        }
        if account
        else None
    )
    if account:
        credential = connection.execute(
            select(AdapterCredential.blob).where(
                AdapterCredential.user_id == item.user_id,
                AdapterCredential.platform == item.platform,
                AdapterCredential.principal == account["principal"],
            )
        ).scalar_one_or_none()
        config["credential_epoch"] = (
            hashlib.sha256(credential.encode()).hexdigest() if credential else None
        )
    digest = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "text": item.proposal_text or "",
        "bid": item.bid_amount,
        "period_days": item.bid_period_days,
        "currency": job["currency"],
        "billing_unit": job["job_type"],
        "platform": item.platform,
        "job_external_id": job["external_id"],
        "destination": job["url"],
        "request_type": item.request_type or "job",
        "account_id": account["id"] if account else None,
        "bidder_id": (item.submission_result or {}).get("bidder_id") or ((account["settings"] or {}).get("bidder_id") if account else None),
        "agency_member": (item.submission_result or {}).get("on_behalf_of") or ((account["settings"] or {}).get("on_behalf_of") if account else None),
        "connects_required": (item.submission_result or {}).get("connects_required", 0),
        "account_digest": digest,
        "ambiguous_account": len(accounts) > 1,
    }


def require_snapshot(db, item):
    from fastapi import HTTPException

    actual = snapshot(db.connection(), item)
    if (
        not item.approved_snapshot
        or item.approved_snapshot != actual
        or actual.get("ambiguous_account")
    ):
        raise HTTPException(
            409,
            "approved content, destination or account changed (or approval predates snapshots); return to review and approve the current version",
        )
    return actual


def select_review_account(db, item, account_id=None):
    """Resolve and persist the reviewer's account before capturing approval."""
    from fastapi import HTTPException
    selected = account_id if account_id is not None else item.platform_account_id
    query = db.query(PlatformAccount).filter(
        PlatformAccount.user_id == item.user_id,
        PlatformAccount.platform == item.platform,
        PlatformAccount.enabled.is_(True), PlatformAccount.mode != "disabled")
    if selected is not None:
        query = query.filter(PlatformAccount.id == selected)
    accounts = query.order_by(PlatformAccount.id).limit(2).all()
    if selected is not None and not accounts:
        raise HTTPException(409, "selected account is unavailable; choose an enabled account for this platform")
    if len(accounts) > 1:
        raise HTTPException(409, "choose a platform account before approving this proposal")
    if account_id is not None and account_id != item.platform_account_id:
        # Identity defaults from a previous account must not follow a new choice.
        result = dict(item.submission_result or {})
        result.pop("bidder_id", None)
        result.pop("on_behalf_of", None)
        item.submission_result = result
    item.platform_account_id = accounts[0].id if accounts else None
