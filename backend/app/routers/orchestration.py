from ..pagination import PageLimit, PageOffset
from ..schemas import BooleanValidateIn
"""CRUD for saved search profiles and connected platform accounts."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_user, get_owned, scoped
from ..boolquery import BooleanQueryError, parse_boolean_query
from ..database import get_db
from ..models import (KeywordGroup, PlatformAccount, SearchFilter,
                      SearchProfile, User)
from ..schemas import (PlatformAccountIn, PlatformAccountOut, SearchProfileIn,
                       SearchProfileOut)

router = APIRouter(prefix="/api", tags=["orchestration"])


# --- Search profiles ---

@router.get("/search-profiles", response_model=list[SearchProfileOut])
def list_search_profiles(db: Session = Depends(get_db), user: User = Depends(get_current_user), limit: PageLimit = 100, offset: PageOffset = 0):
    return scoped(db, SearchProfile, user).order_by(SearchProfile.id).offset(offset).limit(limit).all()


def _validate_refs(body: SearchProfileIn, db: Session, user: User):
    """Referenced keyword group / filter must exist and belong to the caller
    (404 either way — don't leak existence). Null clears the reference and is
    always allowed (SearchProfileIn is a full-replacement schema)."""
    if body.keyword_group_id is not None and not get_owned(
            db, KeywordGroup, body.keyword_group_id, user):
        raise HTTPException(404, "keyword group not found")
    if body.filter_id is not None and not get_owned(
            db, SearchFilter, body.filter_id, user):
        raise HTTPException(404, "filter not found")


@router.post("/search-profiles", response_model=SearchProfileOut, status_code=201)
def create_search_profile(body: SearchProfileIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _validate_boolean(body.boolean_query)
    _validate_refs(body, db, user)
    profile = SearchProfile(user_id=user.id, **body.model_dump())
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.put("/search-profiles/{profile_id}", response_model=SearchProfileOut)
def update_search_profile(profile_id: int, body: SearchProfileIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _validate_boolean(body.boolean_query)
    profile = get_owned(db, SearchProfile, profile_id, user)
    if not profile:
        raise HTTPException(404, "search profile not found")
    _validate_refs(body, db, user)
    for k, v in body.model_dump().items():
        setattr(profile, k, v)
    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/search-profiles/{profile_id}", status_code=204)
def delete_search_profile(profile_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    profile = get_owned(db, SearchProfile, profile_id, user)
    if not profile:
        raise HTTPException(404, "search profile not found")
    db.delete(profile)
    db.commit()


@router.post("/search-profiles/validate-boolean", response_model=dict)
def validate_boolean(body: BooleanValidateIn, user: User = Depends(get_current_user)):
    body = body.model_dump()
    """Dry-run a boolean query string; returns parse status (for the builder UI)."""
    query = body.get("query", "")
    try:
        ast = parse_boolean_query(query)
    except BooleanQueryError as exc:
        return {"valid": False, "error": str(exc)}
    return {"valid": True, "ast": repr(ast) if ast else None}


@router.post("/search-profiles/{profile_id}/run-now", response_model=dict)
async def run_search_profile_now(profile_id: int, db: Session = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    """Run discovery for one profile immediately ("Run search now").

    Adapter searches + ingest run inline; proposal generation is handed to
    Celery tasks by the ingest pipeline — this request never blocks on LLM
    work. Manual runs bypass the per-platform pacing lock.
    """
    profile = get_owned(db, SearchProfile, profile_id, user)
    if not profile:
        raise HTTPException(404, "search profile not found")
    from ..discovery import run_profile_discovery
    result = await run_profile_discovery(db, user, profile, respect_pacing=False)
    return result


def _validate_boolean(query: str):
    try:
        parse_boolean_query(query)
    except BooleanQueryError as exc:
        raise HTTPException(422, f"invalid boolean query: {exc}")


# --- Platform accounts ---

@router.get("/accounts", response_model=list[PlatformAccountOut])
def list_accounts(db: Session = Depends(get_db), user: User = Depends(get_current_user), limit: PageLimit = 100, offset: PageOffset = 0):
    return scoped(db, PlatformAccount, user).order_by(PlatformAccount.id).offset(offset).limit(limit).all()


@router.post("/accounts", response_model=PlatformAccountOut, status_code=201)
def create_account(body: PlatformAccountIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if body.platform == "indeed":
        raise HTTPException(422,"Indeed supports manual job import and tracking only; no account connector is implemented")
    db.refresh(user, with_for_update=True)
    if db.query(PlatformAccount.id).filter_by(user_id=user.id, platform=body.platform, principal=body.principal).first():
        raise HTTPException(409, "this platform principal is already enrolled")
    account = PlatformAccount(user_id=user.id, **body.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.put("/accounts/{account_id}", response_model=PlatformAccountOut)
def update_account(account_id: int, body: PlatformAccountIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    account = get_owned(db, PlatformAccount, account_id, user)
    if not account:
        raise HTTPException(404, "account not found")
    if body.platform != account.platform or body.principal != account.principal:
        raise HTTPException(409, "account platform and principal are immutable; enroll a separate account")
    for k, v in body.model_dump().items():
        setattr(account, k, v)
    db.commit()
    db.refresh(account)
    return account


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Serialize deletion with enrollment of the same principal. Vault rows are
    # keyed by principal, not account ID, so deleting the account alone would
    # leave a usable legacy credential and silently revive it on re-enrollment.
    db.refresh(user, with_for_update=True)
    account = get_owned(db, PlatformAccount, account_id, user)
    if not account:
        raise HTTPException(404, "account not found")
    from ..models import AdapterCredential, AuditLog
    db.query(AdapterCredential).filter_by(
        user_id=user.id, platform=account.platform, principal=account.principal
    ).delete(synchronize_session=False)
    db.add(AuditLog(user_id=user.id, action_type="platform_account_deleted",
                    platform=account.platform, detail={"account_id": account.id,
                                                       "credentials_revoked": True}))
    db.delete(account)
    db.commit()
