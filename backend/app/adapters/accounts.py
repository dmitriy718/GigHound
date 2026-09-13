"""Deterministic credential selection for adapter entry points."""

from ..models import PlatformAccount
from .base import AdapterAuthError


def default_principal(db, user_id, platform, legacy):
    accounts = (
        db.query(PlatformAccount)
        .filter(
            PlatformAccount.user_id == user_id,
            PlatformAccount.platform == platform,
            PlatformAccount.enabled.is_(True),
            PlatformAccount.mode != "disabled",
        )
        .order_by(PlatformAccount.id)
        .limit(2)
        .all()
    )
    if len(accounts) > 1:
        raise AdapterAuthError(
            "multiple active accounts require explicit account selection"
        )
    return accounts[0].principal if accounts else legacy


def selected_principal(db, user_id, platform, account_id, legacy):
    """API selection: owned/enabled account or an unambiguous legacy default."""
    from fastapi import HTTPException
    if account_id is None:
        try:
            return default_principal(db, user_id, platform, legacy)
        except AdapterAuthError:
            raise HTTPException(409, "multiple enabled accounts; provide account_id")
    account = db.query(PlatformAccount).filter(
        PlatformAccount.id == account_id, PlatformAccount.user_id == user_id,
        PlatformAccount.platform == platform, PlatformAccount.enabled.is_(True),
        PlatformAccount.mode != "disabled").first()
    if account is None:
        raise HTTPException(404, "enabled platform account not found")
    return account.principal
