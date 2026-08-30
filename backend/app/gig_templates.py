"""Gig template system: Fiverr + Upwork Project Catalog template logic.

Fiverr taxonomy below is a seed of the top ~50 categories (as of 2026);
refresh from Fiverr's seller dashboard when it drifts — the taxonomy is
data, not code: patch FIVERR_CATEGORIES or POST updated lists to the API.
"""
import logging
import re

from sqlalchemy.orm import Session

from . import llm
from .models import GigTemplate

log = logging.getLogger(__name__)

# Seed: top Fiverr categories → subcategories (update as Fiverr's taxonomy drifts)
FIVERR_CATEGORIES: dict[str, list[str]] = {
    "Graphics & Design": ["Logo Design", "Brand Style Guides", "Illustration",
                          "Web & Mobile Design", "Social Media Design", "Presentation Design"],
    "Programming & Tech": ["Website Development", "WordPress", "E-Commerce Development",
                           "Web Programming", "Mobile Apps", "Chatbots", "AI Development",
                           "Data Processing", "Cybersecurity", "DevOps & Cloud"],
    "Digital Marketing": ["SEO", "Social Media Marketing", "Content Marketing",
                          "Email Marketing", "PPC Advertising", "Marketing Strategy"],
    "Writing & Translation": ["Articles & Blog Posts", "Copywriting", "Technical Writing",
                              "Proofreading & Editing", "Translation", "Website Content"],
    "Video & Animation": ["Video Editing", "Explainer Videos", "Animation",
                          "Intros & Outros", "Subtitles & Captions"],
    "Music & Audio": ["Voice Over", "Mixing & Mastering", "Podcast Production",
                      "Sound Effects", "Songwriters"],
    "Business": ["Virtual Assistant", "Data Entry", "Market Research",
                 "Business Plans", "Financial Consulting", "Project Management"],
    "AI Services": ["AI Chatbots", "AI Content", "Prompt Engineering", "AI Model Fine-Tuning"],
    "Photography": ["Product Photography", "Photo Editing", "Portraits"],
}

TITLE_MAX = 80
TAG_MAX = 5


def seo_title_score(title: str, keywords: list[str] | None = None) -> dict:
    """Heuristic SEO score (0-100) for a gig title."""
    issues, score = [], 100
    n = len(title)
    if n > TITLE_MAX:
        issues.append(f"over {TITLE_MAX} chars ({n})")
        score -= 30
    elif n < 30:
        issues.append("short titles rank worse — aim for 50-80 chars")
        score -= 15
    if keywords:
        t = title.lower()
        hits = sum(1 for k in keywords if k.lower() in t)
        if hits == 0:
            issues.append("no target keyword in title")
            score -= 30
        score -= max(0, (len(keywords) - hits)) * 5
    if re.search(r"\b(best|amazing|top quality|cheap)\b", title.lower()):
        issues.append("avoid hype words — Fiverr deprioritizes them")
        score -= 10
    if not re.match(r"^i will ", title.lower()):
        issues.append("Fiverr convention: start with 'I will ...'")
        score -= 5
    return {"score": max(0, score), "issues": issues}


def validate_fiverr_template(data: dict) -> list[str]:
    """Returns a list of validation problems (empty = valid)."""
    problems = []
    title = data.get("title", "")
    if not title:
        problems.append("title required")
    elif len(title) > TITLE_MAX:
        problems.append(f"title over {TITLE_MAX} chars")
    if data.get("category") and data["category"] not in FIVERR_CATEGORIES:
        problems.append(f"unknown category '{data['category']}' (see /api/gigs/taxonomy/fiverr)")
    subs = FIVERR_CATEGORIES.get(data.get("category"), [])
    if data.get("subcategory") and subs and data["subcategory"] not in subs:
        problems.append(f"unknown subcategory '{data['subcategory']}'")
    tags = data.get("tags") or []
    if len(tags) > TAG_MAX:
        problems.append(f"max {TAG_MAX} tags")
    for tier in ("basic", "standard", "premium"):
        t = (data.get("pricing") or {}).get(tier)
        if t is None:
            continue
        if not t.get("price") or t["price"] < 5:
            problems.append(f"{tier} tier: price must be ≥ $5")
        if not t.get("delivery_days"):
            problems.append(f"{tier} tier: delivery_days required")
    desc = data.get("description") or {}
    for section in ("hook", "what_you_get", "why_me", "cta"):
        if not desc.get(section):
            problems.append(f"description section '{section}' missing")
    return problems


def validate_upwork_catalog_template(data: dict) -> list[str]:
    problems = []
    for field in ("title", "category", "description"):
        if not data.get(field):
            problems.append(f"{field} required")
    if not (data.get("deliverables") or []):
        problems.append("deliverables[] required")
    if not data.get("price") or data["price"] < 5:
        problems.append("price must be ≥ $5")
    return problems


async def generate_faqs(gig_type: str, title: str, count: int = 4) -> list[dict]:
    """LLM-generated FAQs with deterministic fallback."""
    if llm.llm_available():
        try:
            parsed, _ = await llm.complete_json(
                "You write FAQ entries for freelance gig listings. Return JSON: "
                '{"faqs": [{"question": "...", "answer": "..."}]}',
                f"Gig: {title} ({gig_type}). Write {count} FAQs buyers actually ask.",
                temperature=0.6, max_tokens=600,
            )
            faqs = parsed.get("faqs", [])
            if faqs:
                return faqs[:count]
        except Exception as exc:  # noqa: BLE001
            log.warning("FAQ generation failed (%s); using fallback", exc)
    return [
        {"question": "What do you need from me to get started?",
         "answer": f"Just your project brief and any existing materials related to {gig_type}."},
        {"question": "How many revisions are included?",
         "answer": "Each tier lists its revision count — I always make sure you're happy with the result."},
        {"question": "Can you deliver faster than the listed time?",
         "answer": "Usually yes — message me before ordering and I'll confirm a rush timeline."},
        {"question": "Do you offer ongoing support after delivery?",
         "answer": "Yes, I include 7 days of post-delivery support on every package."},
    ][:count]


def create_template(db: Session, user_id: int, platform: str, name: str, template_json: dict,
                    auto_publish: bool = False) -> tuple[GigTemplate | None, list[str]]:
    """Validate + persist. Returns (template, problems)."""
    if platform == "fiverr":
        problems = validate_fiverr_template(template_json)
    elif platform == "upwork":
        problems = validate_upwork_catalog_template(template_json)
    else:
        problems = []  # other platforms: free-form templates
    if problems:
        return None, problems
    tpl = GigTemplate(user_id=user_id, platform=platform, name=name,
                      template_json=template_json, auto_publish=auto_publish)
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl, []
