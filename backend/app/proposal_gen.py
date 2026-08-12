"""AI proposal generation pipeline.

    analyze job → skill-gap/portfolio match → platform-tuned generation
    → anti-detection pass → bid calculation → confidence score

LLM-backed when LLM_API_KEY is configured; otherwise a deterministic local
composer produces solid drafts (used in tests and offline dev). Either way,
output shape is identical and everything lands in the review queue — no
path from this module submits anything.
"""
import logging
import re
import time

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from . import llm
from .antidetect import humanize
from .models import Job, ProfileTemplate
from .scoring import COMPLEXITY_TERMS, detect_red_flags, estimate_complexity, estimate_hours

log = logging.getLogger(__name__)

CONFIDENCE_REVIEW_FLOOR = 50.0

PLATFORM_PROFILES = {
    "upwork": {
        "system": (
            "You are a top-rated freelancer writing an Upwork proposal. "
            "Rules: 2-3 paragraphs, 100-150 words max. Ask exactly 1 smart clarifying question. "
            "Reference a specific detail from the job. Never use generic openings. "
            "Tone: confident, concise, slightly casual. No bullet points."
        ),
        "max_words": 150,
    },
    "freelancer": {
        "system": (
            "You are a technical freelancer bidding on Freelancer.com. "
            "Rules: 200-300 words, include milestone breakdown, delivery timeline, "
            "technical approach. Professional but approachable. "
            "Reference portfolio pieces by name."
        ),
        "max_words": 300,
    },
    "fiverr": {
        "system": (
            "You are responding to a Fiverr buyer request. "
            "Rules: 2-3 sentences, ultra-brief. State custom offer price and turnaround. "
            "Friendly, direct, no fluff."
        ),
        "max_words": 60,
    },
    "linkedin": {
        "system": (
            "You are writing a professional cover letter for a LinkedIn job application. "
            "Rules: 150-200 words, aligned to resume. Formal but warm. "
            "Connect 2 job requirements to specific experience."
        ),
        "max_words": 200,
    },
    "indeed": {
        "system": (
            "You are writing a professional cover letter for an Indeed application. "
            "Rules: 150-200 words, aligned to resume. Formal but warm. "
            "Connect 2 job requirements to specific experience."
        ),
        "max_words": 200,
    },
    "guru": {
        "system": (
            "You are quoting on Guru.com. Hybrid style: 150-200 words, "
            "mix of technical approach and personal pitch."
        ),
        "max_words": 200,
    },
    "peopleperhour": {
        "system": (
            "You are sending a PeoplePerHour proposal. Hybrid style: 150-200 words, "
            "mix of technical approach and personal pitch."
        ),
        "max_words": 200,
    },
}

_ANALYSIS_SYSTEM = (
    "You analyze freelance job posts. Extract: required_skills[], deliverables[], "
    "timeline, budget_mentioned, client_pain_points[], tone (professional|casual|technical), "
    "missing_info[], red_flags[]. Return JSON only."
)


# ---------------- a) job analysis ----------------

async def analyze_job(job: Job) -> tuple[dict, dict]:
    """Returns (analysis, meta). LLM JSON analysis, heuristic fallback offline."""
    user = f"TITLE: {job.title}\n\nDESCRIPTION:\n{job.description}\n\nBUDGET: {job.budget_min}-{job.budget_max} {job.currency}"
    if llm.llm_available():
        try:
            analysis, meta = await llm.complete_json(_ANALYSIS_SYSTEM, user, temperature=0.2)
            return analysis, meta
        except Exception as exc:  # noqa: BLE001 — fall back to heuristics
            log.warning("LLM job analysis failed (%s); using heuristics", exc)
    return _heuristic_analysis(job), {"model": "heuristic-offline", "latency_ms": 0}


def _heuristic_analysis(job: Job) -> dict:
    text = f"{job.title}\n{job.description}"
    t = text.lower()
    skills = list({*(job.skills or []),
                   *(term for term in COMPLEXITY_TERMS if term in t)})[:12]
    deliverables = re.findall(r"(?:deliverables?|scope)[:\-]?\s*([^\n.]+)", text, re.IGNORECASE)[:5]
    word_count = len((job.description or "").split())
    missing = []
    if not (job.budget_min or job.budget_max):
        missing.append("budget")
    if not job.apply_deadline and not re.search(r"\b(deadline|by \w+ \d+|within \d+ (days?|weeks?))\b", t):
        missing.append("timeline")
    if word_count < 60:
        missing.append("scope detail")
    tone = "technical" if len(skills) >= 3 else ("casual" if re.search(r"\b(guys|hey|cool|awesome)\b", t) else "professional")
    flags, _ = detect_red_flags(text, job.budget_usd_max or job.budget_usd_min)
    return {
        "required_skills": skills,
        "deliverables": deliverables,
        "timeline": None,
        "budget_mentioned": bool(job.budget_min or job.budget_max),
        "client_pain_points": re.findall(r"(?:struggling with|need help|problem is|tired of)\s+([^.\n]+)", t)[:3],
        "tone": tone,
        "missing_info": missing,
        "red_flags": flags,
    }


# ---------------- b) skill gap & portfolio match ----------------

def skill_portfolio_match(db: Session, job: Job, analysis: dict) -> dict:
    """Top-3 portfolio pieces + per-piece overlap %, strengths, and gaps."""
    from .orchestrator import select_portfolio_items  # lazy: avoid circular import

    items = select_portfolio_items(db, job, limit=3)
    required = [s.lower() for s in (analysis.get("required_skills") or job.skills or [])]
    have = {s.lower() for s in (job.skills or [])}
    portfolio_match = {}
    for item in items:
        hay = " ".join([item.title, *(item.tags or [])]).lower()
        overlap = [s for s in required if fuzz.partial_ratio(s, hay) >= 75]
        pct = round(100 * len(overlap) / len(required)) if required else 0
        portfolio_match[str(item.id)] = {"title": item.title, "overlap_pct": pct,
                                         "matched_skills": overlap}
    strengths = sorted(have & set(required))
    gaps = sorted(set(required) - have - {s for pm in portfolio_match.values() for s in pm["matched_skills"]})
    return {
        "items": items,
        "portfolio_match": portfolio_match,
        "strengths": strengths[:8],
        "gaps": gaps[:8],  # surfaced so the draft never overpromises
    }


# ---------------- c) platform-specific generation ----------------

async def _generate_with_llm(platform: str, job: Job, analysis: dict,
                             match: dict, rate_line: str, bid_hint: str,
                             few_shot: list, temperature: float) -> tuple[str, dict]:
    profile = PLATFORM_PROFILES.get(platform, PLATFORM_PROFILES["guru"])
    examples = ""
    if few_shot:
        examples = "\n\nWinning example proposals for style reference:\n" + "\n---\n".join(
            t.text[:600] for t in few_shot
        )
    portfolio_lines = "; ".join(
        pm["title"] for pm in match["portfolio_match"].values()
    ) or "available on request"
    user = (
        f"JOB TITLE: {job.title}\n"
        f"JOB DESCRIPTION: {job.description[:2000]}\n"
        f"ANALYSIS: skills={analysis.get('required_skills')}, "
        f"pain_points={analysis.get('client_pain_points')}, tone={analysis.get('tone')}\n"
        f"MY MATCHING STRENGTHS: {match['strengths']}\n"
        f"SKILL GAPS (do NOT claim these): {match['gaps']}\n"
        f"PORTFOLIO PIECES: {portfolio_lines}\n"
        f"RATE CONTEXT: {rate_line}. {bid_hint}\n"
        f"Write the proposal now (max {profile['max_words']} words)."
        f"{examples}"
    )
    result = await llm.complete(profile["system"], user, temperature=temperature, max_tokens=700)
    return result["text"].strip(), result


def _generate_offline(platform: str, job: Job, analysis: dict, match: dict,
                      rate_line: str, opening: str, bid_amount: float | None) -> str:
    """Deterministic local composer — used when no LLM key is configured."""
    profile = PLATFORM_PROFILES.get(platform, PLATFORM_PROFILES["guru"])
    max_words = profile["max_words"]
    skills = ", ".join((analysis.get("required_skills") or job.skills or ["this stack"])[:4])
    portfolio = list(match["portfolio_match"].values())
    pf_line = (f"Closest match in my portfolio: {portfolio[0]['title']}."
               if portfolio else "Portfolio links ready on request.")
    pain = (analysis.get("client_pain_points") or [""])[0]
    pain_line = f"It sounds like {pain.strip()} — I've untangled that exact situation before." if pain else ""

    if platform == "fiverr":
        price = f"${bid_amount:g}" if bid_amount else "a fair price"
        text = (f"{opening} I can deliver \"{job.title[:60]}\" — {skills} is my daily work. "
                f"Custom offer: {price}, delivered in {job.bid_period_days or 3} days. "
                f"{pf_line}")
    elif platform in ("linkedin", "indeed"):
        reqs = (analysis.get("required_skills") or ["the role"])[:2]
        text = (
            f"{opening}\n\nYour need for {reqs[0] if reqs else 'this work'} maps directly to my "
            f"recent project work, and my experience with {skills} covers the second requirement "
            f"as well. {pf_line} {pain_line}\n\n"
            f"I'd welcome a short conversation about fit. Available to start within the week."
        )
    elif platform == "freelancer":
        text = (
            f"{opening}\n\nApproach for \"{job.title[:60]}\": milestone 1 — requirements locked "
            f"and architecture agreed (day 1-2); milestone 2 — core build with {skills} "
            f"(week 1); milestone 3 — polish, tests, handover (final days). {pf_line} {pain_line}\n\n"
            f"Timeline: {job.bid_period_days or 14} days. {rate_line}. "
            f"Happy to walk through the plan on a quick call."
        )
    else:  # upwork / guru / peopleperhour default shape
        text = (
            f"{opening}\n\n"
            f"For \"{job.title[:60]}\", my read: you need {skills} done cleanly, not a science "
            f"project. {pf_line} {pain_line}\n\n"
            f"One question before I lock my estimate — what does success look like two weeks "
            f"after delivery? Your answer shapes how I'd sequence the work."
        )
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(",;") + "."
    return text


# ---------------- e) bid calculation ----------------

def calculate_bid(db: Session, job: Job, analysis: dict) -> tuple[float | None, int | None, str]:
    """Returns (amount, period_days, rationale)."""
    from .orchestrator import pick_rate

    rate = pick_rate(db, job)
    if job.job_type == "hourly":
        if rate and rate.hourly_rate:
            return rate.hourly_rate, None, f"rate card: {rate.skill_category} @ ${rate.hourly_rate:g}/hr"
        return None, None, "no rate card entry matched; set manually"
    if job.platform == "fiverr":
        base = (rate.fixed_min if rate and rate.fixed_min else None) or job.budget_usd_min or 50
        return float(base), 3, "fiverr custom offer: basic tier price"
    # fixed price: estimated hours × hourly × complexity multiplier (1.0–1.5)
    text = f"{job.title}\n{job.description}"
    complexity = estimate_complexity(text)
    hours = estimate_hours(complexity, text)
    hourly = (rate.hourly_rate if rate and rate.hourly_rate else 50.0)
    multiplier = 1.0 + 0.5 * (complexity / 15.0)
    estimate = hours * hourly * multiplier
    if rate and rate.fixed_min:
        estimate = max(estimate, rate.fixed_min)
    if job.budget_usd_max and estimate > job.budget_usd_max * 1.2:
        estimate = job.budget_usd_max  # cap near stated budget
    period = max(3, min(60, int(hours / 6)))
    rationale = (f"{hours:.0f}h est. × ${hourly:g}/hr × {multiplier:.2f} complexity "
                 f"= ${estimate:,.0f}")
    return round(estimate, 2), period, rationale


# ---------------- f) full pipeline ----------------

async def generate(db: Session, job: Job, *, sender_name: str = "GigHound user",
                   few_shot: list | None = None, temperature: float = 0.7) -> dict:
    """Generate a complete proposal package for a job."""
    started = time.monotonic()
    analysis, analysis_meta = await analyze_job(job)
    match = skill_portfolio_match(db, job, analysis)

    from .orchestrator import pick_rate
    rate = pick_rate(db, job)
    rate_line = (f"${rate.hourly_rate:g}/hr" if rate and rate.hourly_rate
                 else "rate on request")
    bid_amount, bid_days, bid_rationale = calculate_bid(db, job, analysis)
    job.bid_period_days = bid_days  # local hint for composers (not persisted)

    meta = dict(analysis_meta)
    used_llm = False
    if llm.llm_available():
        try:
            draft, meta = await _generate_with_llm(
                job.platform, job, analysis, match, rate_line,
                f"Suggested bid: ${bid_amount:g}" if bid_amount else "Suggest a bid.",
                few_shot or [], temperature,
            )
            used_llm = True
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM generation failed (%s); composing offline", exc)
    if not used_llm:
        from .antidetect import pick_opening
        opening = pick_opening(title=job.title,
                               tech=(analysis.get("required_skills") or [""])[0])
        draft = _generate_offline(job.platform, job, analysis, match, rate_line,
                                  opening, bid_amount)

    # platform template override: user's own pitch template wins as a wrapper
    # only when no LLM draft exists — LLM output is already platform-tuned.
    if not used_llm:
        tpl = (db.query(ProfileTemplate)
               .filter(ProfileTemplate.platform == job.platform)
               .order_by(ProfileTemplate.created_at).first())
        if tpl and tpl.pitch_template and "{job_title}" in tpl.pitch_template:
            pass  # template handled by offline composer's structure; skip legacy rendering

    anti = humanize(draft, platform=job.platform, title=job.title,
                    tech=(analysis.get("required_skills") or [""])[0])

    # confidence: analysis richness + skill coverage − red flags − gaps
    required = analysis.get("required_skills") or []
    coverage = len(match["strengths"]) / len(required) if required else 0.5
    confidence = 55 + 25 * coverage
    confidence -= 8 * len(analysis.get("red_flags") or [])
    confidence -= 4 * len(match["gaps"])
    confidence -= 10 * (not analysis.get("budget_mentioned"))
    confidence += 5 if used_llm else 0
    confidence = round(max(5.0, min(98.0, confidence)), 1)

    latency_ms = int((time.monotonic() - started) * 1000)
    return {
        "draft_text": anti["raw_text"],
        "humanized_text": anti["humanized_text"],
        "typing_plan": anti["typing_plan"],
        "sentence_stats": anti["sentence_stats"],
        "bid_amount": bid_amount,
        "bid_period_days": bid_days,
        "bid_rationale": bid_rationale,
        "portfolio_item_ids": [p.id for p in match["items"]],
        "portfolio_match": match["portfolio_match"],
        "analysis": {**analysis, "strengths": match["strengths"], "gaps": match["gaps"]},
        "confidence": confidence,
        "needs_review": confidence < CONFIDENCE_REVIEW_FLOOR,
        "llm_model": meta.get("model", "offline-composer"),
        "latency_ms": latency_ms,
    }
