from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import PortfolioItem, ProfileTemplate, RateCardEntry
from ..schemas import (PortfolioItemIn, PortfolioItemOut, ProfileTemplateIn,
                       ProfileTemplateOut, RateCardIn, RateCardOut)

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


# --- AI generation of profile pitch templates ---

_PROFILE_GEN_SYSTEM = (
    "You write pitch profile templates for a freelancer on {platform}. "
    "The template MUST keep these placeholder tokens verbatim so the app can "
    "fill them per job: {{job_title}}, {{skills}}, {{portfolio_links}}, {{rate_line}}. "
    "Write 120-180 words, first person, confident but not salesy. "
    "Return only the template text."
)


@router.post("/templates/generate", response_model=dict)
async def generate_profile_template(body: dict):
    """Generate a pitch template via the configured text provider (Ollama
    by default). Returns draft text — the user reviews, then saves through
    the normal CRUD endpoints. Never auto-saves."""
    platform = body.get("platform", "upwork")
    notes = body.get("notes", "")
    from ..textgen import LLMUnavailable, generateText

    user = (f"Platform: {platform}. Style notes: {notes or 'none'}. "
            f"Write the template now.")
    try:
        result = await generateText(
            _PROFILE_GEN_SYSTEM.format(platform=platform), user,
            temperature=body.get("temperature"), max_tokens=body.get("max_tokens"),
            timeout=body.get("timeout"),
        )
        return {"text": result["text"], "model": result["model"],
                "provider": result["provider"], "latency_ms": result["latency_ms"],
                "offline": False}
    except LLMUnavailable as exc:
        # deterministic offline fallback
        text = (
            "Hi — I read \"{job_title}\" and it maps closely to my recent work.\n\n"
            "I work primarily with {skills}. Relevant pieces: {portfolio_links}.\n\n"
            "On approach: I scope tightly, communicate daily, and deliver in milestones "
            "so you always know where things stand. My rate for this kind of work: {rate_line}.\n\n"
            "Happy to outline a concrete plan for your deliverables before you commit."
        )
        return {"text": text, "model": "offline-fallback", "provider": "none",
                "latency_ms": 0, "offline": True, "warning": str(exc)}


# --- Profile templates (per-platform pitch styles) ---

@router.get("/templates", response_model=list[ProfileTemplateOut])
def list_templates(platform: str | None = None, db: Session = Depends(get_db)):
    q = db.query(ProfileTemplate)
    if platform:
        q = q.filter(ProfileTemplate.platform == platform)
    return q.all()


@router.post("/templates", response_model=ProfileTemplateOut, status_code=201)
def create_template(body: ProfileTemplateIn, db: Session = Depends(get_db)):
    tpl = ProfileTemplate(**body.model_dump())
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.put("/templates/{tpl_id}", response_model=ProfileTemplateOut)
def update_template(tpl_id: int, body: ProfileTemplateIn, db: Session = Depends(get_db)):
    tpl = db.get(ProfileTemplate, tpl_id)
    if not tpl:
        raise HTTPException(404, "template not found")
    for k, v in body.model_dump().items():
        setattr(tpl, k, v)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.delete("/templates/{tpl_id}", status_code=204)
def delete_template(tpl_id: int, db: Session = Depends(get_db)):
    tpl = db.get(ProfileTemplate, tpl_id)
    if not tpl:
        raise HTTPException(404, "template not found")
    db.delete(tpl)
    db.commit()


# --- Portfolio ---

@router.get("/portfolio", response_model=list[PortfolioItemOut])
def list_portfolio(db: Session = Depends(get_db)):
    return db.query(PortfolioItem).all()


@router.post("/portfolio", response_model=PortfolioItemOut, status_code=201)
def create_portfolio(body: PortfolioItemIn, db: Session = Depends(get_db)):
    item = PortfolioItem(**body.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/portfolio/{item_id}", response_model=PortfolioItemOut)
def update_portfolio(item_id: int, body: PortfolioItemIn, db: Session = Depends(get_db)):
    item = db.get(PortfolioItem, item_id)
    if not item:
        raise HTTPException(404, "portfolio item not found")
    for k, v in body.model_dump().items():
        setattr(item, k, v)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/portfolio/{item_id}", status_code=204)
def delete_portfolio(item_id: int, db: Session = Depends(get_db)):
    item = db.get(PortfolioItem, item_id)
    if not item:
        raise HTTPException(404, "portfolio item not found")
    db.delete(item)
    db.commit()


# --- Rate card ---

@router.get("/rate-card", response_model=list[RateCardOut])
def list_rate_card(db: Session = Depends(get_db)):
    return db.query(RateCardEntry).all()


@router.post("/rate-card", response_model=RateCardOut, status_code=201)
def create_rate_card(body: RateCardIn, db: Session = Depends(get_db)):
    entry = RateCardEntry(**body.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.put("/rate-card/{entry_id}", response_model=RateCardOut)
def update_rate_card(entry_id: int, body: RateCardIn, db: Session = Depends(get_db)):
    entry = db.get(RateCardEntry, entry_id)
    if not entry:
        raise HTTPException(404, "rate card entry not found")
    for k, v in body.model_dump().items():
        setattr(entry, k, v)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/rate-card/{entry_id}", status_code=204)
def delete_rate_card(entry_id: int, db: Session = Depends(get_db)):
    entry = db.get(RateCardEntry, entry_id)
    if not entry:
        raise HTTPException(404, "rate card entry not found")
    db.delete(entry)
    db.commit()
