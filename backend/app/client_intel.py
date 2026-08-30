"""Client intelligence (Phase 3.1 / 3.4).

Client identity: adapters populate `client_info.client_id` (and `name`) when
the platform API exposes a stable identifier; otherwise we fall back to a
(country, rating, total_spent bucket) composite — the best signal available
from anonymized search results. `client_history_for_job` aggregates this
user's past proposals for the same client; `compute_bid_advice` turns
proposal-count competition into a go/no-go recommendation at queue time.
"""
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Job, ProposalQueueItem

log = logging.getLogger(__name__)


def _spent_bucket(spent) -> str:
    if spent is None:
        return "unknown"
    if spent <= 0:
        return "0"
    if spent < 1000:
        return "<1k"
    if spent < 10000:
        return "1-10k"
    if spent < 50000:
        return "10-50k"
    return "50k+"


def _identity_key(client_info: dict) -> tuple | None:
    """Platform-agnostic identity key from a client_info dict, or None when
    there is nothing to identify the client by."""
    ci = client_info or {}
    if ci.get("client_id"):
        return ("id", str(ci["client_id"]))
    if ci.get("name"):
        return ("name", str(ci["name"]).strip().lower())
    if ci.get("country") or ci.get("rating") is not None or ci.get("total_spent") is not None:
        return ("composite", ci.get("country"), ci.get("rating"),
                _spent_bucket(ci.get("total_spent")))
    return None


def client_identity(job: Job) -> tuple | None:
    key = _identity_key(job.client_info)
    return (job.platform, *key) if key else None


def client_key_for(client_info: dict, platform: str) -> str | None:
    """Stable string key for a client identity — stored on `Job.client_key`
    (indexed) at write time so history lookups are keyed queries. None when
    the client cannot be identified."""
    key = _identity_key(client_info)
    if key is None:
        return None
    return "|".join(str(p) for p in (platform, *key))


def client_history_for_job(db: Session, user_id: int, job: Job) -> dict | None:
    """Aggregate this user's past proposals for the job's client.

    Keyed lookup on the indexed `jobs.client_key` (populated at write time
    from client_info) joined to proposal_queue — no full-table scan of
    client_info blobs. Proposals on THIS job are excluded — history means
    prior engagements. Returns None when the client has never been seen (or
    cannot be identified)."""
    if not job.client_key:
        return None
    rows = (db.query(ProposalQueueItem.outcome, func.count())
            .join(Job, ProposalQueueItem.job_id == Job.id)
            .filter(ProposalQueueItem.user_id == user_id,
                    Job.client_key == job.client_key,
                    Job.id != job.id)
            .group_by(ProposalQueueItem.outcome)
            .all())
    counts = {outcome: int(n) for outcome, n in rows}
    past = sum(counts.values())
    if not past:
        return None
    return {"past_proposals": past,
            "hired": counts.get("hired", 0),
            "rejected": counts.get("rejected", 0),
            "ghosted": counts.get("ghosted", 0)}


def format_client_history(history: dict) -> str:
    """One-line operator context for the generation prompt (kept OUTSIDE the
    untrusted <job_posting> tags)."""
    return (f"you've bid {history['past_proposals']}x for this client before: "
            f"{history['hired']} hired, {history['rejected']} rejected, "
            f"{history['ghosted']} ghosted. Adjust tone and ask accordingly.")


def compute_bid_advice(job: Job) -> dict | None:
    """Go/no-go from competition level + job quality. None when the platform
    didn't report a proposals count."""
    n = job.proposals_count
    if n is None:
        return None
    if n > 25 and job.quality_score < 70:
        return {"recommendation": "skip",
                "reason": (f"{n} competing proposals and quality score "
                           f"{job.quality_score:.0f} (<70) — poor odds for the effort")}
    if n > 15:
        return {"recommendation": "caution",
                "reason": f"{n} competing proposals — only bid with a standout, specific pitch"}
    return {"recommendation": "bid",
            "reason": f"only {n} competing proposals — good odds for a tailored pitch"}
