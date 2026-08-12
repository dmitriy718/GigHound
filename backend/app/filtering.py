"""Search fine-tuning: match a Job against a SearchFilter preset."""
from datetime import datetime, timezone

from rapidfuzz import fuzz

from .scoring import to_usd


def _aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def job_matches_filter(job, flt) -> tuple[bool, list[str]]:
    """Returns (matched, reasons_for_exclusion)."""
    reasons = []

    if flt.platforms and job.platform not in flt.platforms:
        reasons.append(f"platform '{job.platform}' not selected")
    if flt.job_types and job.job_type and job.job_type not in flt.job_types:
        reasons.append(f"job type '{job.job_type}' not selected")
    if flt.experience_levels and job.experience_level and job.experience_level not in flt.experience_levels:
        reasons.append(f"experience level '{job.experience_level}' not selected")
    if flt.work_arrangements and job.work_arrangement and job.work_arrangement not in flt.work_arrangements:
        reasons.append(f"work arrangement '{job.work_arrangement}' not selected")

    # Per-platform budget range (normalized to USD)
    budgets = {b["platform"]: b for b in (flt.budgets or [])}
    b = budgets.get(job.platform)
    if b:
        usd_min = to_usd(job.budget_min, job.currency)
        usd_max = to_usd(job.budget_max, job.currency)
        ref = usd_max if usd_max is not None else usd_min
        bmin = to_usd(b.get("min"), b.get("currency", "USD"))
        bmax = to_usd(b.get("max"), b.get("currency", "USD"))
        if ref is not None:
            if bmin is not None and ref < bmin:
                reasons.append(f"budget ${ref} below min ${bmin}")
            if bmax is not None and usd_min is not None and usd_min > bmax:
                reasons.append(f"budget ${usd_min} above max ${bmax}")

    # Client history
    cf = flt.client_filters or {}
    ci = job.client_info or {}
    if cf.get("payment_verified") and not ci.get("payment_verified"):
        reasons.append("client payment not verified")
    if cf.get("min_hire_rate") is not None and (ci.get("hire_rate") or 0) < cf["min_hire_rate"]:
        reasons.append(f"hire rate {ci.get('hire_rate')} below {cf['min_hire_rate']}")
    if cf.get("min_total_spent") is not None and (ci.get("total_spent") or 0) < cf["min_total_spent"]:
        reasons.append(f"client spend below ${cf['min_total_spent']}")
    if cf.get("countries") and ci.get("country") and ci["country"] not in cf["countries"]:
        reasons.append(f"client country '{ci.get('country')}' not allowed")

    # Time filters
    now = datetime.now(timezone.utc)
    if flt.posted_within_hours is not None:
        posted = _aware(job.posted_at)
        if not posted or (now - posted).total_seconds() / 3600 > flt.posted_within_hours:
            reasons.append(f"not posted within {flt.posted_within_hours}h")
    if flt.apply_deadline_within_hours is not None:
        dl = _aware(job.apply_deadline)
        if dl and (dl - now).total_seconds() / 3600 > flt.apply_deadline_within_hours:
            reasons.append(f"deadline beyond {flt.apply_deadline_within_hours}h")

    # Language requirements (fuzzy: job must list at least one required language, or none specified)
    if flt.languages and job.languages:
        if not any(
            fuzz.ratio(req.lower(), have.lower()) >= 85
            for req in flt.languages
            for have in job.languages
        ):
            reasons.append("language requirement not met")

    # Oversaturation
    if flt.max_proposals is not None and (job.proposals_count or 0) > flt.max_proposals:
        reasons.append(f"{job.proposals_count} proposals > max {flt.max_proposals}")

    # Quality threshold
    if job.quality_score < flt.quality_threshold:
        reasons.append(f"score {job.quality_score} below threshold {flt.quality_threshold}")

    return (not reasons), reasons
