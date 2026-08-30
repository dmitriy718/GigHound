"""Auth endpoints: register, login (rate-limited), me, logout (AD-2).

Login attempts are rate-limited per email+IP via a Redis token bucket;
when Redis is down the limiter is a graceful no-op.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import (create_access_token, get_current_user, hash_password,
                    verify_password)
from ..cache import cache
from ..config import ALLOW_REGISTRATION
from ..database import get_db
from ..models import User
from ..schemas import (AccountDeleteIn, LoginIn, PasswordChangeIn, RegisterIn,
                       TokenOut, UserOut)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger(__name__)

LOGIN_ATTEMPT_LIMIT = 5        # attempts per window per email+IP
LOGIN_WINDOW_SECONDS = 300


def _check_login_rate(email: str, ip: str) -> None:
    """429 when the login attempt bucket overflows; no-op if Redis is down."""
    if cache._r is None:
        return
    key = f"login_attempts:{email}:{ip}"
    try:
        attempts = cache._r.incr(key)
        if attempts == 1:
            cache._r.expire(key, LOGIN_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001 — limiter must never block logins
        log.warning("login rate limiter unavailable; skipping")
        return
    if attempts > LOGIN_ATTEMPT_LIMIT:
        raise HTTPException(429, "too many login attempts")


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    if not ALLOW_REGISTRATION:
        raise HTTPException(403, "registration is disabled")
    email = body.email.strip().lower()
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
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "account disabled")
    return TokenOut(access_token=create_access_token(user), user=user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/logout", response_model=dict)
def logout(user: User = Depends(get_current_user)):
    """Stateless JWTs are discarded client-side; this endpoint only
    acknowledges the logout (no server-side revocation in v1)."""
    return {"status": "ok"}


@router.post("/password", response_model=dict)
def change_password(body: PasswordChangeIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Change the account password. Existing tokens stay valid (stateless JWTs);
    the client should re-login at its convenience."""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    db.commit()
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
