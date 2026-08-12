from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import Keyword, KeywordGroup
from ..schemas import KeywordGroupIn, KeywordGroupOut
from ..skills_taxonomy import suggest_skills

router = APIRouter(prefix="/api", tags=["keywords"])


@router.get("/keyword-groups", response_model=list[KeywordGroupOut])
def list_groups(db: Session = Depends(get_db)):
    return db.query(KeywordGroup).options(selectinload(KeywordGroup.keywords)).all()


@router.post("/keyword-groups", response_model=KeywordGroupOut, status_code=201)
def create_group(body: KeywordGroupIn, db: Session = Depends(get_db)):
    group = KeywordGroup(name=body.name, service_type=body.service_type)
    group.keywords = [Keyword(term=k.term, kind=k.kind, weight=k.weight) for k in body.keywords]
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


@router.put("/keyword-groups/{group_id}", response_model=KeywordGroupOut)
def update_group(group_id: int, body: KeywordGroupIn, db: Session = Depends(get_db)):
    group = db.get(KeywordGroup, group_id)
    if not group:
        raise HTTPException(404, "keyword group not found")
    group.name = body.name
    group.service_type = body.service_type
    group.keywords = [Keyword(term=k.term, kind=k.kind, weight=k.weight) for k in body.keywords]
    db.commit()
    db.refresh(group)
    return group


@router.delete("/keyword-groups/{group_id}", status_code=204)
def delete_group(group_id: int, db: Session = Depends(get_db)):
    group = db.get(KeywordGroup, group_id)
    if not group:
        raise HTTPException(404, "keyword group not found")
    db.delete(group)
    db.commit()


@router.get("/skills/suggest")
def skills_suggest(platform: str | None = None, q: str = ""):
    return {"suggestions": suggest_skills(platform, q)}
