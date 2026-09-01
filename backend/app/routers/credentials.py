"""Credential enrollment for platform accounts.

This is the product path that writes into the Fernet vault
(`adapters/vault.py`): users enroll secrets through the API, the vault row
is stored under the account's `credential_ref` (auto-generated as
`vault://{platform}/{principal}` when the account has none), and secret
values are never returned or logged — status exposes key names only.

Recognized secret keys per platform:
  * freelancer: `access_token` (required), `refresh_token` (opt)
  * stealth platforms (fiverr, peopleperhour, guru):
    `storage_state_json` (a Playwright storage_state JSON string — the
    preferred path, seeded straight into the worker's browser context), OR
    raw `username` + `password` for the worker's login flow.
    Password-based login is a FALLBACK only: it is challenge-prone
    (CAPTCHA/2FA) and may escalate to a human at run time.
  * upwork supports BOTH credential types: API tokens (`access_token`,
    `refresh_token`) OR a browser session per the stealth rules above
    (the worker drives upwork through the browser, so a stealth session is
    what most tenants enroll).
"""
import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..adapters.freelancer import FreelancerAdapter
from ..adapters.vault import CredentialVault
from ..auth import get_current_user, get_owned
from ..database import get_db
from ..models import AdapterCredential, AuditLog, PlatformAccount, User
from ..schemas import CredentialStatusOut, CredentialsIn, OAuthCompleteIn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["credentials"])

_OAUTH_PLATFORMS = ("freelancer", "upwork")
# stealth platforms served by the browser worker (worker/config.py
# SUPPORTED_PLATFORMS minus API-driven ones); upwork is BOTH oauth + stealth
_STEALTH_PLATFORMS = ("fiverr", "peopleperhour", "guru", "upwork")
_OAUTH_KEYS = {"access_token", "refresh_token"}
_STEALTH_KEYS = {"storage_state_json", "username", "password"}

_DEFAULT_REDIRECT_URI = "http://localhost:5173/oauth/freelancer/callback"


def _freelancer_oauth_config() -> tuple[str, str, str]:
    client_id = os.getenv("FREELANCER_CLIENT_ID", "")
    client_secret = os.getenv("FREELANCER_CLIENT_SECRET", "")
    redirect_uri = os.getenv("FREELANCER_REDIRECT_URI", _DEFAULT_REDIRECT_URI)
    return client_id, client_secret, redirect_uri


def _get_account(db: Session, account_id: int, user: User) -> PlatformAccount:
    account = get_owned(db, PlatformAccount, account_id, user)
    if not account:
        raise HTTPException(404, "account not found")
    return account


def _ensure_credential_ref(db: Session, account: PlatformAccount) -> str:
    """Generate and persist `vault://{platform}/{principal}` when unset.

    The ref is derived from the account's own (platform, principal), so it
    is unique per (user, platform, principal) — the same triple the vault's
    uniqueness constraint keys on, hence no collisions.
    """
    if not account.credential_ref:
        account.credential_ref = f"vault://{account.platform}/{account.principal}"
        db.commit()
    return account.credential_ref


def _validate_oauth_secrets(platform: str, secrets: dict):
    unknown = set(secrets) - _OAUTH_KEYS
    if unknown:
        raise HTTPException(422, f"unknown credential keys for {platform}: {sorted(unknown)}")
    if not secrets.get("access_token"):
        raise HTTPException(422, f"{platform}: 'access_token' is required")


def _validate_stealth_secrets(platform: str, secrets: dict):
    unknown = set(secrets) - _STEALTH_KEYS
    if unknown:
        raise HTTPException(422, f"unknown credential keys for {platform}: {sorted(unknown)}")
    has_state = bool(secrets.get("storage_state_json"))
    has_userpass = bool(secrets.get("username")) or bool(secrets.get("password"))
    if has_state and has_userpass:
        raise HTTPException(
            422, "provide either 'storage_state_json' or 'username'+'password', not both")
    if has_state:
        try:
            state = json.loads(secrets["storage_state_json"])
        except (ValueError, TypeError):
            raise HTTPException(422, "'storage_state_json' is not valid JSON") from None
        if not isinstance(state, dict):
            raise HTTPException(422, "'storage_state_json' must decode to a JSON object")
    elif has_userpass:
        if not (secrets.get("username") and secrets.get("password")):
            raise HTTPException(422, "'username' and 'password' must be provided together")
    else:
        raise HTTPException(
            422, f"{platform}: provide 'storage_state_json' or 'username'+'password'")


def _validate_dual_secrets(platform: str, secrets: dict):
    """Platform in BOTH sets (upwork): API tokens OR a browser session."""
    unknown = set(secrets) - (_OAUTH_KEYS | _STEALTH_KEYS)
    if unknown:
        raise HTTPException(422, f"unknown credential keys for {platform}: {sorted(unknown)}")
    if secrets.get("access_token"):
        stealth = {k: v for k, v in secrets.items() if k in _STEALTH_KEYS}
        if stealth:  # mixed enrollment: the stealth half must still be coherent
            _validate_stealth_secrets(platform, stealth)
        return
    _validate_stealth_secrets(platform, secrets)


def _validate_secrets(platform: str, secrets: dict) -> dict:
    if not secrets:
        raise HTTPException(422, "secrets must not be empty")
    if platform in _OAUTH_PLATFORMS and platform in _STEALTH_PLATFORMS:
        _validate_dual_secrets(platform, secrets)
    elif platform in _OAUTH_PLATFORMS:
        _validate_oauth_secrets(platform, secrets)
    elif platform in _STEALTH_PLATFORMS:
        _validate_stealth_secrets(platform, secrets)
    else:
        raise HTTPException(422, f"credential enrollment not supported for '{platform}'")
    if any(not isinstance(v, str) or not v for v in secrets.values()):
        raise HTTPException(422, "secret values must be non-empty strings")
    return secrets


def _audit(db: Session, user: User, action: str, account: PlatformAccount, keys: list[str]):
    db.add(AuditLog(user_id=user.id, action_type=action, platform=account.platform,
                    detail={"account_id": account.id, "keys": sorted(keys)}))
    db.commit()


@router.post("/{account_id}/credentials", status_code=204)
def enroll_credentials(account_id: int, body: CredentialsIn,
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    account = _get_account(db, account_id, user)
    secrets = _validate_secrets(account.platform, body.secrets)
    _ensure_credential_ref(db, account)
    CredentialVault(db, user.id).store(account.platform, account.principal, secrets)
    _audit(db, user, "credentials_enrolled", account, list(secrets))
    log.info("credentials enrolled for account %d (%s/%s), keys=%s",
             account.id, account.platform, account.principal, sorted(secrets))


@router.get("/{account_id}/credentials/status", response_model=CredentialStatusOut)
def credential_status(account_id: int, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Enrollment status only — secret values are never returned."""
    account = _get_account(db, account_id, user)
    row = (db.query(AdapterCredential)
           .filter_by(user_id=user.id, platform=account.platform,
                      principal=account.principal)
           .first())
    if not account.credential_ref or not row:
        return CredentialStatusOut(enrolled=False, keys=[], updated_at=None)
    creds = CredentialVault(db, user.id).load(account.platform, account.principal)
    if not creds:
        return CredentialStatusOut(enrolled=False, keys=[], updated_at=None)
    return CredentialStatusOut(enrolled=True, keys=sorted(creds),
                               updated_at=row.updated_at)


@router.delete("/{account_id}/credentials", status_code=204)
def delete_credentials(account_id: int, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    account = _get_account(db, account_id, user)
    vault = CredentialVault(db, user.id)
    creds = vault.load(account.platform, account.principal) or {}
    vault.delete(account.platform, account.principal)
    account.credential_ref = ""
    db.commit()
    _audit(db, user, "credentials_deleted", account, list(creds))
    log.info("credentials deleted for account %d (%s/%s)",
             account.id, account.platform, account.principal)


# --- Freelancer OAuth 2.0 (authorization-code flow) ---

@router.get("/{account_id}/oauth/freelancer/start", response_model=dict)
async def freelancer_oauth_start(account_id: int, db: Session = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    account = _get_account(db, account_id, user)
    if account.platform != "freelancer":
        raise HTTPException(400, "account is not a freelancer account")
    client_id, _, redirect_uri = _freelancer_oauth_config()
    if not client_id:
        raise HTTPException(
            501, "Freelancer OAuth is not configured on this deployment — "
                 "set FREELANCER_CLIENT_ID/FREELANCER_CLIENT_SECRET")
    adapter = FreelancerAdapter(db, user.id)
    try:
        authorize_url = adapter.build_authorize_url(client_id, redirect_uri)
    finally:
        await adapter.close()
    return {"authorize_url": authorize_url}


@router.post("/{account_id}/oauth/freelancer/complete", status_code=204)
async def freelancer_oauth_complete(account_id: int, body: OAuthCompleteIn,
                                    db: Session = Depends(get_db),
                                    user: User = Depends(get_current_user)):
    account = _get_account(db, account_id, user)
    if account.platform != "freelancer":
        raise HTTPException(400, "account is not a freelancer account")
    client_id, client_secret, redirect_uri = _freelancer_oauth_config()
    if not (client_id and client_secret):
        raise HTTPException(
            501, "Freelancer OAuth is not configured on this deployment — "
                 "set FREELANCER_CLIENT_ID/FREELANCER_CLIENT_SECRET")
    adapter = FreelancerAdapter(db, user.id)
    try:
        tokens = await adapter.exchange_code(
            client_id, client_secret, body.code, body.redirect_uri or redirect_uri)
    finally:
        await adapter.close()
    # exchange_code persists under principal "default"; re-store under the
    # account's own principal when it differs so credential_ref stays true.
    if account.principal != "default":
        CredentialVault(db, user.id).store(account.platform, account.principal, tokens)
    _ensure_credential_ref(db, account)
    _audit(db, user, "credentials_enrolled", account, list(tokens))
    log.info("freelancer OAuth completed for account %d (principal=%s)",
             account.id, account.principal)
