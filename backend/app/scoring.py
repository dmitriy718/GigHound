"""Job Quality Score (0-100) engine — v2.

Components (max 100):
    keyword_match        25   primary exact weighted (15) + secondary fuzzy (10)
    budget_realism       25   budget vs. estimated hours × market rate
    client_verification  20   payment verified +9, identity verified +5, past hires ≤6
    description_quality  20   >100 words +6, deliverables +8, tech requirements +6, vague −20
    urgency_ratio        10   budget / (complexity × timeline days), higher = better
    red_flag_penalty   −30 ea  capped at −60

A negative-keyword hit zeroes the score and excludes the job. Jobs below the
user-defined threshold (default 40) are auto-archived by the ingest pipeline.
"""
import re
from datetime import datetime, timezone

from rapidfuzz import fuzz

DEFAULT_QUALITY_THRESHOLD = 40.0
DEFAULT_MARKET_RATE = 50.0  # USD/hour fallback when no rate card entry matches

# --- Currency normalization (static fallback rates → USD) ---
USD_RATES = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66,
    "INR": 0.012, "BRL": 0.18, "PLN": 0.25, "UAH": 0.024, "PHP": 0.017,
}


def to_usd(amount, currency):
    if amount is None:
        return None
    return round(amount * USD_RATES.get((currency or "USD").upper(), 1.0), 2)


# --- Complexity heuristics ---
COMPLEXITY_TERMS = {
    "full-stack": 3, "full stack": 3, "microservices": 3, "kubernetes": 3,
    "machine learning": 3, "ai": 1, "llm": 2, "blockchain": 3, "devops": 2,
    "aws": 2, "gcp": 2, "azure": 2, "react": 1, "next.js": 1, "node": 1,
    "python": 1, "django": 1, "fastapi": 1, "postgresql": 1, "graphql": 1,
    "typescript": 1, "mobile": 2, "ios": 2, "android": 2, "api": 1,
    "integration": 1, "migration": 2, "architecture": 2, "security": 2,
    "real-time": 2, "websocket": 1, "ci/cd": 2, "terraform": 2,
}

RED_FLAG_PATTERNS = [
    (r"unlimited revisions?", "unlimited revisions"),
    (r"test task|unpaid (test|trial|sample)|free (sample|trial) work", "test task before hire"),
    (r"work for (exposure|review)|great exposure|portfolio building|opportunity for exposure", "work for exposure/review"),
    (r"no upfront|no milestone|payment after (completion|delivery)|paid when (done|finished)", "no upfront/milestone payment"),
    (r"will pay (in|with) (equity|shares)|revenue share only", "equity/deferred compensation"),
    (r"student project|school project|homework|budget project", "student/budget project"),
]

# Word-boundary lookups for COMPLEXITY_TERMS: "ai" must not match "said",
# "api" must not match "capital". Compiled once, matched against lowered text.
_COMPLEXITY_RES = {
    term: re.compile(r"\b" + re.escape(term) + r"\b") for term in COMPLEXITY_TERMS
}


def _term_re(term: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(term.lower()) + r"\b")

VAGUE_PHRASES = [
    "need a website", "need an app", "simple project", "quick job",
    "easy task", "asap", "help me", "looking for someone",
]

DELIVERABLE_PATTERNS = r"(deliverables?|scope of work|milestones?|you will (build|create|deliver)|what we need|what you'll do)"


def estimate_complexity(text: str) -> int:
    """Rough complexity score 0-15 from technical term density."""
    t = text.lower()
    return min(15, sum(w for term, w in COMPLEXITY_TERMS.items()
                       if _COMPLEXITY_RES[term].search(t)))


def estimate_hours(complexity: int, text: str) -> float:
    """Estimated effort in hours: base 10h + 10h per complexity point, capped."""
    t = text.lower()
    hours = 10 + complexity * 10
    if re.search(r"\b(mvp|full product|entire|complete platform)\b", t):
        hours *= 1.5
    if re.search(r"\b(small|minor|tweak|fix|bug)\b", t) and complexity <= 3:
        hours *= 0.5
    return min(400.0, float(hours))


def score_keyword_match(text: str, primary: list, secondary: list, negative: list) -> tuple[float, bool]:
    """Returns (points 0-25, excluded). Negative hit → excluded."""
    t = text.lower()
    for kw in negative:
        term = kw if isinstance(kw, str) else kw.term
        # word-boundary: negative "php" must not kill "graphPHP"
        if _term_re(term).search(t):
            return 0.0, True
    points = 0.0
    if primary:
        per = 15.0 / len(primary)
        for kw in primary:
            if kw.term.lower() in t:  # exact (case-insensitive) match, weighted
                points += per * kw.weight
    if secondary:
        per = 10.0 / len(secondary)
        for kw in secondary:  # fuzzy / semantic-ish match
            if fuzz.partial_ratio(kw.term.lower(), t) >= 80:
                points += per
    return round(min(25.0, points), 1), False


def score_budget_realism(budget_usd, est_hours: float, market_rate: float,
                         job_type: str | None) -> tuple[float, list[str]]:
    """0-25 points: budget vs. estimated_hours × market_rate."""
    flags = []
    if budget_usd is None:
        return 10.0, flags  # unknown budget: neutral-low
    if job_type == "hourly":
        # budget is the hourly rate itself
        ratio = budget_usd / market_rate
    else:
        ratio = budget_usd / (est_hours * market_rate)
    if ratio < 0.25:
        flags.append("unrealistic budget")
        return 2.0, flags
    if ratio < 0.6:
        return 8.0, flags
    if ratio < 1.0:
        return 15.0, flags
    if ratio < 2.5:
        return 25.0, flags
    return 21.0, flags  # suspiciously high: slight discount, possible bait


def score_client_verification(client: dict) -> float:
    """0-20 points: payment verified +9, identity verified +5, past hires ≤6."""
    if not client:
        return 3.0
    pts = 0.0
    if client.get("payment_verified"):
        pts += 9.0
    if client.get("identity_verified"):
        pts += 5.0
    hires = client.get("past_hires") or 0
    pts += min(6.0, hires / 10.0)  # 60+ past hires → full points
    return round(min(20.0, pts), 1)


def score_description_quality(text: str) -> float:
    """0-20 points: >100 words +6, deliverables +8, tech requirements +6; vague −20."""
    if not text:
        return 0.0
    t = text.lower()
    pts = 0.0
    if len(text.split()) > 100:
        pts += 6.0
    if re.search(DELIVERABLE_PATTERNS, t):
        pts += 8.0
    tech_hits = sum(1 for rx in _COMPLEXITY_RES.values() if rx.search(t))
    if tech_hits:
        pts += min(6.0, tech_hits * 1.5)
    vague_hits = sum(1 for p in VAGUE_PHRASES if p in t)
    if vague_hits >= 2 and len(text.split()) < 60:
        pts -= 20.0  # vague/generic
    return round(max(0.0, min(20.0, pts)), 1)


def score_urgency_ratio(budget_usd, complexity: int, posted_at, apply_deadline) -> float:
    """0-10 points: budget / (complexity × timeline_days) — higher is better.

    Falls back to post-recency scoring when budget or deadline is missing.
    """
    now = datetime.now(timezone.utc)
    pts = 0.0
    if budget_usd and apply_deadline:
        dl = apply_deadline if apply_deadline.tzinfo else apply_deadline.replace(tzinfo=timezone.utc)
        days_left = max(0.25, (dl - now).total_seconds() / 86400)
        ratio = budget_usd / (max(1, complexity) * days_left)
        if ratio >= 100:
            pts = 10.0
        elif ratio >= 50:
            pts = 8.0
        elif ratio >= 20:
            pts = 6.0
        elif ratio >= 5:
            pts = 3.0
        else:
            pts = 1.0
    else:
        pts = 4.0  # neutral
    # freshness bonus/penalty
    if posted_at:
        pa = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
        age_h = (now - pa).total_seconds() / 3600
        if age_h <= 1:
            pts += 0.0  # recency handled by hot-job logic, not score
        elif age_h > 168:
            pts -= 2.0
    return round(max(0.0, min(10.0, pts)), 1)


def detect_red_flags(text: str, budget_usd=None, posted_urgency_words=True) -> tuple[list[str], float]:
    """Returns (flag labels, penalty). −30 each, capped at −60."""
    t = (text or "").lower()
    flags, penalty = [], 0.0
    for pattern, label in RED_FLAG_PATTERNS:
        if re.search(pattern, t):
            flags.append(label)
            penalty += 30.0
    # urgent + low budget combo
    if budget_usd is not None and budget_usd < 200 and re.search(
        r"\b(urgent|asap|immediately|today)\b", t
    ):
        flags.append("urgent + low budget")
        penalty += 30.0
    return flags, min(60.0, penalty)


def detect_bait_and_switch(title: str, description: str) -> bool:
    """Title promises skilled work but description is vague or unrelated."""
    if not description:
        return False
    t_title, t_desc = title.lower(), description.lower()
    title_tech = {term for term, rx in _COMPLEXITY_RES.items() if rx.search(t_title)}
    desc_tech = {term for term, rx in _COMPLEXITY_RES.items() if rx.search(t_desc)}
    vague = sum(1 for p in VAGUE_PHRASES if p in t_desc)
    return bool(title_tech) and not desc_tech and (vague >= 2 or len(description) < 150)


def compute_quality_score(job, keywords=None, market_rate: float | None = None) -> dict:
    """job: JobIngest-like; keywords: optional list of Keyword-like objects.

    Returns {quality_score, score_breakdown, red_flags}.
    """
    text = f"{job.title}\n{job.description}"
    primary, secondary, negative = [], [], []
    if keywords:
        primary = [k for k in keywords if k.kind == "primary"]
        secondary = [k for k in keywords if k.kind == "secondary"]
        negative = [k for k in keywords if k.kind == "negative"]

    budget_usd = to_usd(job.budget_max or job.budget_min, job.currency)
    complexity = estimate_complexity(text)
    est_hours = estimate_hours(complexity, text)
    rate = market_rate or DEFAULT_MARKET_RATE

    kw_pts, excluded = score_keyword_match(text, primary, secondary, negative)
    if excluded:
        return {
            "quality_score": 0.0,
            "score_breakdown": {"keyword_match": 0, "excluded_by_negative_keyword": 1},
            "red_flags": ["negative keyword match"],
        }

    budget_pts, budget_flags = score_budget_realism(budget_usd, est_hours, rate, job.job_type)
    client = job.client_info.model_dump() if hasattr(job.client_info, "model_dump") else (job.client_info or {})
    client_pts = score_client_verification(client)
    desc_pts = score_description_quality(job.description)
    urgency_pts = score_urgency_ratio(budget_usd, complexity, job.posted_at, job.apply_deadline)
    rf_flags, penalty = detect_red_flags(text, budget_usd)
    if detect_bait_and_switch(job.title, job.description):
        rf_flags.append("bait-and-switch")
        penalty = min(60.0, penalty + 15)

    total = kw_pts + budget_pts + client_pts + desc_pts + urgency_pts - penalty
    return {
        "quality_score": round(max(0.0, min(100.0, total)), 1),
        "score_breakdown": {
            "keyword_match": kw_pts,
            "budget_realism": budget_pts,
            "client_verification": client_pts,
            "description_quality": desc_pts,
            "urgency_ratio": urgency_pts,
            "red_flag_penalty": -penalty,
        },
        "red_flags": budget_flags + rf_flags,
    }
