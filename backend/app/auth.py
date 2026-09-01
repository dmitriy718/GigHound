"""Authentication & tenancy helpers (AD-1, AD-2).

Passwords are hashed with bcrypt directly. API auth is a JWT Bearer
token (HS256, 12h) whose payload is {sub: user_id, email, jti, iat, exp}.
The signing secret comes from GIGHOUND_SECRET_KEY; startup fails fast when
it is unset unless GIGHOUND_DEV_NOAUTH=1, in which case every request runs
as a single implicit dev user so local development needs no login.

Revocation is Redis-backed (jwt:deny:{jti} denylist + jwt:notbefore:{user}
per-user invalidation) and fails open when Redis is down, matching the
cache layer's degradation philosophy.
"""
import hmac
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .cache import cache
from .config import DEV_NOAUTH, SECRET_KEY, WORKER_TOKEN
from .database import get_db
from .models import PlatformAccount, User

log = logging.getLogger(__name__)

ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(hours=12)
DEV_USER_EMAIL = "dev@gighound.local"

_bearer = HTTPBearer(auto_error=False)


def validate_auth_config() -> None:
    """Fail fast at startup when secrets are missing/invalid (unless dev mode)."""
    if not SECRET_KEY and not DEV_NOAUTH:
        raise RuntimeError(
            "GIGHOUND_SECRET_KEY is not set — refusing to start with auth "
            "enabled and no signing secret. Set it, or GIGHOUND_DEV_NOAUTH=1 "
            "for local development without auth."
        )
    if not WORKER_TOKEN and not DEV_NOAUTH:
        raise RuntimeError(
            "GIGHOUND_WORKER_TOKEN is not set — the stealth worker pool would "
            "be unauthenticated. Set it, or GIGHOUND_DEV_NOAUTH=1 for local "
            "development."
        )
    vault_key = os.getenv("GIGHOUND_VAULT_KEY") or os.getenv("GIGHUNTER_VAULT_KEY")
    if not vault_key and not DEV_NOAUTH:
        raise RuntimeError(
            "GIGHOUND_VAULT_KEY is not set — the credential vault would 500 "
            "on first use. Generate one with: python -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "or set GIGHOUND_DEV_NOAUTH=1 for local development."
        )
    if vault_key:
        try:
            Fernet(vault_key.encode())
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "GIGHOUND_VAULT_KEY is not a valid Fernet key (urlsafe base64 "
                "32-byte). Generate one with: python -c \"from "
                "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from exc
    if DEV_NOAUTH:
        log.warning("GIGHOUND_DEV_NOAUTH=1 — auth disabled, all requests run "
                    "as the implicit dev user. Do NOT use in production.")


# ---------------- passwords ----------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:  # malformed hash (or >72-byte input on bcrypt >= 4.1)
        return False


# ---------------- tokens ----------------

def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode + validate a token. Raises jwt.PyJWTError on any failure."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


# ---------------- revocation (Redis-backed, fail-open) ----------------

def revoke_token(token: str) -> None:
    """Denylist a token's jti for its remaining lifetime (logout, password
    change). No-op when Redis is down — fail-open, matching the cache
    layer's degradation philosophy (the token then lives until its exp)."""
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        return
    ttl = int(exp - time.time())
    if ttl > 0:
        cache.set_json(f"jwt:deny:{jti}", 1, ttl=ttl)


def invalidate_user_tokens(user_id: int) -> None:
    """Reject all of the user's tokens minted before now (iat < not-before)."""
    cache.set_json(f"jwt:notbefore:{user_id}", int(time.time()),
                   ttl=int(ACCESS_TOKEN_TTL.total_seconds()))


def _is_token_revoked(payload: dict) -> bool:
    """Denylist + per-user not-before check. Fail-open when Redis is down
    (cache reads are no-ops then): availability wins over revocation,
    matching the codebase's degradation philosophy."""
    jti = payload.get("jti")
    if jti and cache.get_json(f"jwt:deny:{jti}"):
        return True
    notbefore = cache.get_json(f"jwt:notbefore:{payload.get('sub')}")
    iat = payload.get("iat")
    if notbefore and iat and int(iat) < int(notbefore):
        return True
    return False


def get_user_from_token(db: Session, token: str | None) -> User | None:
    """Resolve a bearer token to an active user, or None (used by the WS endpoint)."""
    if not token or not SECRET_KEY:
        return None
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return None
    if _is_token_revoked(payload):
        return None
    try:
        user_id = int(payload.get("sub") or 0)
    except (TypeError, ValueError):
        return None
    user = db.get(User, user_id)
    if not user or not user.is_active:
        return None
    return user


# ---------------- dependencies ----------------

def get_or_create_dev_user(db: Session) -> User:
    """The single implicit tenant for GIGHOUND_DEV_NOAUTH=1 local development."""
    user = db.query(User).filter(User.email == DEV_USER_EMAIL).first()
    if not user:
        user = User(email=DEV_USER_EMAIL, password_hash=hash_password("dev-noauth"),
                    display_name="Dev User")
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: the authenticated tenant for this request."""
    if DEV_NOAUTH:
        return get_or_create_dev_user(db)
    user = get_user_from_token(db, creds.credentials if creds else None)
    if user is None:
        raise HTTPException(401, "invalid or missing credentials",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


# ---------------- stealth worker auth (AD-4) ----------------
# The worker pool authenticates with a shared deployment-level token, not a
# user JWT: it serves every tenant, so worker endpoints resolve tenancy from
# the task/row itself (or an explicit user_id), never from the token.

def is_worker_token(creds: HTTPAuthorizationCredentials | None) -> bool:
    return bool(WORKER_TOKEN and creds
                and hmac.compare_digest(creds.credentials, WORKER_TOKEN))


def get_worker(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """FastAPI dependency: worker-token-only gate for mutation endpoints."""
    if DEV_NOAUTH and not WORKER_TOKEN:
        return "dev-worker"
    if is_worker_token(creds):
        return "worker"
    raise HTTPException(401, "invalid or missing worker token",
                        headers={"WWW-Authenticate": "Bearer"})


def get_worker_or_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    """Dual-auth dependency: returns None for a valid worker token (worker =
    cross-tenant principal), otherwise the authenticated user (UI path)."""
    if is_worker_token(creds):
        return None
    return get_current_user(creds, db)


# ---------------- tenancy scoping (AD-1) ----------------

def scoped(db: Session, model, user: User):
    """Query factory: only rows owned by `user`."""
    return db.query(model).filter(model.user_id == user.id)


def get_owned(db: Session, model, pk: int, user: User):
    """Fetch a row by pk, but only if the current user owns it (else None → 404)."""
    obj = db.get(model, pk)
    if obj is None or obj.user_id != user.id:
        return None
    return obj


# ---------------- platform kill switches ----------------

def _active_accounts(db: Session, user_id: int, platform: str) -> list[PlatformAccount]:
    return (db.query(PlatformAccount)
            .filter(PlatformAccount.user_id == user_id,
                    PlatformAccount.platform == platform,
                    PlatformAccount.enabled.is_(True),
                    PlatformAccount.mode != "disabled")
            .all())


def platform_enabled(db: Session, user_id: int, platform: str) -> bool:
    """Kill switch: False when the user has PlatformAccount row(s) for the
    platform and none of them is active (enabled and mode != 'disabled').
    No account row = allowed (default-on for platforms without accounts)."""
    accounts = (db.query(PlatformAccount)
                .filter(PlatformAccount.user_id == user_id,
                        PlatformAccount.platform == platform)
                .all())
    if not accounts:
        return True
    return any(a.enabled and a.mode != "disabled" for a in accounts)


def platform_account_settings(db: Session, user_id: int, platform: str) -> dict:
    """Settings of the user's first enabled PlatformAccount for a platform ({} if none)."""
    accounts = _active_accounts(db, user_id, platform)
    if not accounts:
        return {}
    return dict(accounts[0].settings or {})
