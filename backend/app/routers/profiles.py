from ..pagination import PageLimit, PageOffset
from ..schemas import TemplateGenerateIn
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_user, get_owned, scoped
from ..database import get_db
from ..models import PortfolioItem, ProfileTemplate, RateCardEntry, User
from ..ratelimit import check_llm_gen_rate
from ..schemas import (PortfolioItemIn, PortfolioItemOut, ProfileTemplateIn,
                       ProfileTemplateOut, RateCardIn, RateCardOut)

router = APIRouter(prefix="/api/profiles", tags=["profiles"])

from ..writing_voice import WritingVoice, load_voice, voice_context
from ..writing_voice import ApplicationStyle, TONES, load_style


@router.get("/application-tones")
def application_tones(user: User = Depends(get_current_user)):
    return {"tones": [{"id": key, "label": label, "description": description} for key, (label, description) in TONES.items()],
            "evidence": "Research informs clarity and job relevance; individual presets have not been proven to increase hires."}


def _owned_style_job(db, user, job_id):
    from ..models import Job
    if get_owned(db, Job, job_id, user) is None:
        raise HTTPException(404, "job not found")


@router.get("/jobs/{job_id}/writing-style", response_model=ApplicationStyle)
def get_application_style(job_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _owned_style_job(db, user, job_id)
    return load_style(db, user.id, job_id)


@router.put("/jobs/{job_id}/writing-style", response_model=ApplicationStyle)
def save_application_style(job_id: int, body: ApplicationStyle, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..adapters.vault import StateStore
    _owned_style_job(db, user, job_id)
    StateStore(db, user.id).set("writing", f"job_style:{job_id}", body.model_dump())
    return body


@router.get("/writing-voice", response_model=WritingVoice)
def get_writing_voice(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return load_voice(db, user.id)


@router.put("/writing-voice", response_model=WritingVoice)
def save_writing_voice(body: WritingVoice, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..adapters.vault import StateStore
    StateStore(db, user.id).set("writing", "voice", body.model_dump())
    return body


# --- AI generation of profile pitch templates ---

_PROFILE_GEN_SYSTEM = (
    "You write pitch profile templates for a freelancer on {platform}. "
    "The template MUST keep these placeholder tokens verbatim (double braces) "
    "so the app can fill them per job: {{client_name}}, {{job_title}}, "
    "{{deliverable}}, {{portfolio_piece}}, {{clarifying_question}}, "
    "{{rate_line}}, {{your_name}}. "
    "Write 120-180 words, first person, confident but not salesy. "
    "Never invent experience, results, ratings or availability. "
    "Return only the template text."
)


@router.post("/templates/generate", response_model=dict)
async def generate_profile_template(body: TemplateGenerateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    body = body.model_dump()
    """Generate a pitch template via the configured text provider (Ollama
    by default). Returns draft text — the user reviews, then saves through
    the normal CRUD endpoints. Never auto-saves."""
    platform = body.get("platform", "upwork")
    notes = body.get("notes", "")
    from ..textgen import LLMUnavailable, generateText

    prompt = (f"Platform: {platform}. Style notes: {notes or 'none'}. "
              f"Write the template now." + voice_context(load_voice(db, user.id)))
    check_llm_gen_rate(user)
    try:
        result = await generateText(
            _PROFILE_GEN_SYSTEM.replace("{platform}", platform), prompt,
            temperature=body.get("temperature"), max_tokens=body.get("max_tokens"),
            timeout=body.get("timeout"),
        )
        return {"text": result["text"], "model": result["model"],
                "provider": result["provider"], "latency_ms": result["latency_ms"],
                "offline": False}
    except LLMUnavailable as exc:
        # deterministic offline fallback
        text = (
            "Hi {{client_name}} — I read \"{{job_title}}\".\n\n"
            "Proposed deliverable: {{deliverable}}. Relevant evidence to review: {{portfolio_piece}}.\n\n"
            "Before agreeing to scope, timing or price, I would confirm: {{clarifying_question}}\n"
            "— {{your_name}}"
        )
        return {"text": text, "model": "offline-fallback", "provider": "none",
                "latency_ms": 0, "offline": True, "warning": str(exc)}


# --- Profile templates (per-platform pitch styles) ---

@router.get("/templates", response_model=list[ProfileTemplateOut])
def list_templates(platform: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user), limit: PageLimit = 100, offset: PageOffset = 0):
    q = scoped(db, ProfileTemplate, user)
    if platform:
        q = q.filter(ProfileTemplate.platform == platform)
    return q.order_by(ProfileTemplate.id).offset(offset).limit(limit).all()


@router.post("/templates", response_model=ProfileTemplateOut, status_code=201)
def create_template(body: ProfileTemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = ProfileTemplate(user_id=user.id, **body.model_dump())
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.put("/templates/{tpl_id}", response_model=ProfileTemplateOut)
def update_template(tpl_id: int, body: ProfileTemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = get_owned(db, ProfileTemplate, tpl_id, user)
    if not tpl:
        raise HTTPException(404, "template not found")
    for k, v in body.model_dump().items():
        setattr(tpl, k, v)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.delete("/templates/{tpl_id}", status_code=204)
def delete_template(tpl_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = get_owned(db, ProfileTemplate, tpl_id, user)
    if not tpl:
        raise HTTPException(404, "template not found")
    db.delete(tpl)
    db.commit()


# --- Portfolio ---

@router.get("/portfolio", response_model=list[PortfolioItemOut])
def list_portfolio(db: Session = Depends(get_db), user: User = Depends(get_current_user), limit: PageLimit = 100, offset: PageOffset = 0):
    return scoped(db, PortfolioItem, user).order_by(PortfolioItem.id).offset(offset).limit(limit).all()


@router.post("/portfolio", response_model=PortfolioItemOut, status_code=201)
def create_portfolio(body: PortfolioItemIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = PortfolioItem(user_id=user.id, **body.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/portfolio/{item_id}", response_model=PortfolioItemOut)
def update_portfolio(item_id: int, body: PortfolioItemIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = get_owned(db, PortfolioItem, item_id, user)
    if not item:
        raise HTTPException(404, "portfolio item not found")
    for k, v in body.model_dump().items():
        setattr(item, k, v)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/portfolio/{item_id}", status_code=204)
def delete_portfolio(item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = get_owned(db, PortfolioItem, item_id, user)
    if not item:
        raise HTTPException(404, "portfolio item not found")
    db.delete(item)
    db.commit()


# --- Rate card ---

@router.get("/rate-card", response_model=list[RateCardOut])
def list_rate_card(db: Session = Depends(get_db), user: User = Depends(get_current_user), limit: PageLimit = 100, offset: PageOffset = 0):
    return scoped(db, RateCardEntry, user).order_by(RateCardEntry.id).offset(offset).limit(limit).all()


@router.post("/rate-card", response_model=RateCardOut, status_code=201)
def create_rate_card(body: RateCardIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    entry = RateCardEntry(user_id=user.id, **body.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.put("/rate-card/{entry_id}", response_model=RateCardOut)
def update_rate_card(entry_id: int, body: RateCardIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    entry = get_owned(db, RateCardEntry, entry_id, user)
    if not entry:
        raise HTTPException(404, "rate card entry not found")
    for k, v in body.model_dump().items():
        setattr(entry, k, v)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/rate-card/{entry_id}", status_code=204)
def delete_rate_card(entry_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    entry = get_owned(db, RateCardEntry, entry_id, user)
    if not entry:
        raise HTTPException(404, "rate card entry not found")
    db.delete(entry)
    db.commit()
