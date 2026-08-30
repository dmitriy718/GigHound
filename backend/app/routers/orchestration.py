"""CRUD for saved search profiles and connected platform accounts."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_user, get_owned, scoped
from ..boolquery import BooleanQueryError, parse_boolean_query
from ..database import get_db
from ..models import PlatformAccount, SearchProfile, User
from ..schemas import (PlatformAccountIn, PlatformAccountOut, SearchProfileIn,
                       SearchProfileOut)

router = APIRouter(prefix="/api", tags=["orchestration"])


# --- Search profiles ---

@router.get("/search-profiles", response_model=list[SearchProfileOut])
def list_search_profiles(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return scoped(db, SearchProfile, user).all()


@router.post("/search-profiles", response_model=SearchProfileOut, status_code=201)
def create_search_profile(body: SearchProfileIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _validate_boolean(body.boolean_query)
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
def validate_boolean(body: dict, user: User = Depends(get_current_user)):
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
    return {"queued": result["queued"], "platforms": result["platforms"]}


def _validate_boolean(query: str):
    try:
        parse_boolean_query(query)
    except BooleanQueryError as exc:
        raise HTTPException(422, f"invalid boolean query: {exc}")


# --- Platform accounts ---

@router.get("/accounts", response_model=list[PlatformAccountOut])
def list_accounts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return scoped(db, PlatformAccount, user).all()


@router.post("/accounts", response_model=PlatformAccountOut, status_code=201)
def create_account(body: PlatformAccountIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
    for k, v in body.model_dump().items():
        setattr(account, k, v)
    db.commit()
    db.refresh(account)
    return account


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    account = get_owned(db, PlatformAccount, account_id, user)
    if not account:
        raise HTTPException(404, "account not found")
    db.delete(account)
    db.commit()
