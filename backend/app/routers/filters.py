from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..cache import cache
from ..database import get_db
from ..filtering import job_matches_filter
from ..models import Job, SearchFilter
from ..schemas import PreviewResult, SearchFilterIn, SearchFilterOut

router = APIRouter(prefix="/api/filters", tags=["filters"])


def _apply(flt: SearchFilter, body: SearchFilterIn):
    data = body.model_dump()
    data["client_filters"] = body.client_filters.model_dump()
    data["budgets"] = [b.model_dump() for b in body.budgets]
    for k, v in data.items():
        setattr(flt, k, v)


@router.get("", response_model=list[SearchFilterOut])
def list_filters(db: Session = Depends(get_db)):
    return db.query(SearchFilter).all()


@router.post("", response_model=SearchFilterOut, status_code=201)
def create_filter(body: SearchFilterIn, db: Session = Depends(get_db)):
    flt = SearchFilter()
    _apply(flt, body)
    db.add(flt)
    db.commit()
    db.refresh(flt)
    cache.invalidate_prefix("preview:")
    return flt


@router.put("/{filter_id}", response_model=SearchFilterOut)
def update_filter(filter_id: int, body: SearchFilterIn, db: Session = Depends(get_db)):
    flt = db.get(SearchFilter, filter_id)
    if not flt:
        raise HTTPException(404, "filter not found")
    _apply(flt, body)
    db.commit()
    db.refresh(flt)
    cache.invalidate_prefix("preview:")
    return flt


@router.delete("/{filter_id}", status_code=204)
def delete_filter(filter_id: int, db: Session = Depends(get_db)):
    flt = db.get(SearchFilter, filter_id)
    if not flt:
        raise HTTPException(404, "filter not found")
    db.delete(flt)
    db.commit()
    cache.invalidate_prefix("preview:")


@router.post("/{filter_id}/preview", response_model=PreviewResult)
def preview_filter(filter_id: int, db: Session = Depends(get_db)):
    flt = db.get(SearchFilter, filter_id)
    if not flt:
        raise HTTPException(404, "filter not found")

    cache_key = f"preview:{filter_id}"
    cached = cache.get_json(cache_key)
    if cached:
        return cached

    jobs = db.query(Job).filter(Job.status != "archived").all()
    matched, excluded = [], 0
    for job in jobs:
        ok, _ = job_matches_filter(job, flt)
        if ok:
            matched.append(JobOutFromModel(job))
        else:
            excluded += 1
    result = PreviewResult(matched=matched, excluded_count=excluded)
    cache.set_json(cache_key, result.model_dump(mode="json"))
    return result


def JobOutFromModel(job: Job):
    from ..schemas import JobOut

    return JobOut.model_validate(job)
