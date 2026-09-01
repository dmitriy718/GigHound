"""Auth endpoints: register, login (rate-limited), me, logout (AD-2).

Failed logins are rate-limited per email+IP and per IP via Redis token
buckets; unknown emails run a dummy bcrypt verify so the timing matches the
known-email path. When Redis is down the limiters are graceful no-ops.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import (create_access_token, get_current_user, hash_password,
                    invalidate_user_tokens, revoke_token, verify_password)
from ..cache import cache
from ..config import ALLOW_REGISTRATION
from ..database import get_db
from ..models import User
from ..schemas import (AccountDeleteIn, LoginIn, PasswordChangeIn, RegisterIn,
                       TokenOut, UserOut)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger(__name__)

LOGIN_ATTEMPT_LIMIT = 5        # failures per window per email+IP
LOGIN_IP_ATTEMPT_LIMIT = 20    # failures per window per IP (across accounts)
LOGIN_WINDOW_SECONDS = 300
REGISTER_ATTEMPT_LIMIT = 5     # registrations per window per IP
REGISTER_WINDOW_SECONDS = 3600

# Static precomputed bcrypt hash (generated once, not a real password):
# unknown-email logins verify against it so the response time is
# indistinguishable from the known-email path (user-enumeration oracle).
_DUMMY_HASH = "$2b$12$Bs3.KMMKn/oeyIWHoanj7exMVyG0g86A8J6S.sYiviLaOwL9Yg4D6"


def _bucket_count(key: str) -> int:
    return int(cache._r.get(key) or 0)


def _check_login_rate(email: str, ip: str) -> None:
    """429 when either failure bucket has overflowed; no-op if Redis is down.
    Read-only: buckets are incremented by _record_login_failure, never on
    success (legit logins must not self-429)."""
    if cache._r is None:
        return
    try:
        over = (_bucket_count(f"login_attempts:{email}:{ip}") >= LOGIN_ATTEMPT_LIMIT
                or _bucket_count(f"login_attempts_ip:{ip}") >= LOGIN_IP_ATTEMPT_LIMIT)
    except Exception:  # noqa: BLE001 — limiter must never block logins
        log.warning("login rate limiter unavailable; skipping")
        return
    if over:
        raise HTTPException(429, "too many login attempts")


def _record_login_failure(email: str, ip: str) -> None:
    """Count a failed login in both the per-email+IP and per-IP buckets."""
    if cache._r is None:
        return
    try:
        for key in (f"login_attempts:{email}:{ip}", f"login_attempts_ip:{ip}"):
            attempts = cache._r.incr(key)
            if attempts == 1:
                cache._r.expire(key, LOGIN_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001 — limiter must never block logins
        log.warning("login rate limiter unavailable; skipping")


def _check_register_rate(ip: str) -> None:
    """429 when the per-IP registration bucket overflows; no-op if Redis is down."""
    if cache._r is None:
        return
    key = f"register_attempts:{ip}"
    try:
        attempts = cache._r.incr(key)
        if attempts == 1:
            cache._r.expire(key, REGISTER_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001 — limiter must never block signups
        log.warning("register rate limiter unavailable; skipping")
        return
    if attempts > REGISTER_ATTEMPT_LIMIT:
        raise HTTPException(429, "too many registration attempts")


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    if not ALLOW_REGISTRATION:
        raise HTTPException(403, "registration is disabled")
    ip = request.client.host if request.client else "unknown"
    _check_register_rate(ip)
    email = body.email.strip().lower()
    # The distinguishing 409 leaks which emails are registered (enumeration).
    # Mitigated, not eliminated: the per-IP rate limit above throttles probing,
    # and double-opt-in email verification is the planned real fix.
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "email already registered")
    user = User(email=email, password_hash=hash_password(body.password),
                display_name=body.display_name.strip())
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenOut(access_token=create_access_token(user), user=user)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "unknown"
    _check_login_rate(email, ip)
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        verify_password(body.password, _DUMMY_HASH)  # timing equalizer
        _record_login_failure(email, ip)
        raise HTTPException(401, "invalid email or password")
    if not verify_password(body.password, user.password_hash):
        _record_login_failure(email, ip)
        raise HTTPException(401, "invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "account disabled")
    return TokenOut(access_token=create_access_token(user), user=user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


def _bearer_token(request: Request) -> str | None:
    """Raw JWT from the Authorization header of the current request."""
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else None


@router.post("/logout", response_model=dict)
def logout(request: Request, user: User = Depends(get_current_user)):
    """Revoke the current token (jti denylist until its exp). Fail-open when
    Redis is down: the token then stays valid until it expires naturally."""
    token = _bearer_token(request)
    if token:
        revoke_token(token)
    return {"status": "ok"}


@router.post("/password", response_model=dict)
def change_password(body: PasswordChangeIn, request: Request,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Change the account password. Revokes the current token and invalidates
    every other outstanding token of this user (per-user not-before), so all
    clients must re-login. Fail-open when Redis is down."""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    token = _bearer_token(request)
    if token:
        revoke_token(token)
    invalidate_user_tokens(user.id)
    return {"status": "ok"}


@router.delete("/account", response_model=dict)
def delete_account(body: AccountDeleteIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Permanently delete the account. Every tenant table's user_id FK is
    ON DELETE CASCADE (models + initial migration), so the row delete wipes
    all tenant data. No final AuditLog row is written: audit_log.user_id is a
    non-nullable cascading FK, so the row would be erased by the very delete
    it records — an application-log line is emitted instead."""
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(400, "password is incorrect")
    log.info("account deleted: user_id=%d email=%s", user.id, user.email)
    db.delete(user)
    db.commit()
    return {"status": "deleted"}
