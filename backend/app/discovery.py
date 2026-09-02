"""Scheduled job discovery (Phase 2.1).

For each enabled SearchProfile, derive search terms (positive terms of the
boolean query, else the linked keyword group's primary terms), search the
profile's platforms through the appropriate adapters, and feed results
through the standard ingest pipeline. Per-(user, platform) pacing via a
Redis lock (graceful open when Redis is down); adapter auth errors skip
that platform for the user — a discovery run never crashes the tick.
"""
import logging

from sqlalchemy.orm import Session

from .adapters.base import AdapterAuthError, AdapterError
from .adapters.freelancer import FreelancerAdapter
from .adapters.linkedin import LinkedInJobsAdapter
from .adapters.upwork_agency import UpworkAgencyAdapter
from .auth import platform_enabled
from .boolquery import parse_boolean_query
from .cache import cache
from .ingest import run_ingest
from .models import KeywordGroup, SearchProfile, User
from .platforms import DISCOVERY_PLATFORMS
from .schemas import IngestJobsIn

log = logging.getLogger(__name__)
DISCOVERY_LOCK_SECONDS = 15 * 60  # per-(user, platform) pacing
SEARCHES_PER_PROFILE = 3          # one search per derived term, capped


def _positive_terms(ast) -> list[str]:
    """Positive (non-negated) terms of a parsed boolean-query AST."""
    terms: list[str] = []

    def walk(node, negated: bool):
        if node is None:
            return
        op = node[0]
        if op == "TERM":
            if not negated:
                terms.append(node[1])
        elif op == "NOT":
            walk(node[1], not negated)
        else:  # AND / OR
            walk(node[1], negated)
            walk(node[2], negated)

    walk(ast, False)
    return terms


def search_terms_for_profile(db: Session, profile: SearchProfile) -> list[str]:
    """Discovery query terms: boolean query's positive terms, else the linked
    keyword group's primary terms, else the profile name."""
    if profile.boolean_query and profile.boolean_query.strip():
        terms = _positive_terms(parse_boolean_query(profile.boolean_query))
        if terms:
            return terms[:SEARCHES_PER_PROFILE]
    if profile.keyword_group_id:
        # defense-in-depth: a foreign/missing group is treated as absent
        group = db.get(KeywordGroup, profile.keyword_group_id)
        if group and group.user_id == profile.user_id:
            terms = [k.term for k in group.keywords if k.kind == "primary"]
            if terms:
                return terms[:SEARCHES_PER_PROFILE]
    return [profile.name]


def platforms_for_profile(db: Session, profile: SearchProfile) -> list[str]:
    """Platforms to search: the linked filter's platform list intersected
    with the supported discovery platforms; all of them when unrestricted."""
    selected: list[str] = []
    if profile.filter_id:
        from .models import SearchFilter
        # defense-in-depth: a foreign/missing filter is treated as absent
        flt = db.get(SearchFilter, profile.filter_id)
        if flt and flt.user_id == profile.user_id and flt.platforms:
            selected = [p for p in flt.platforms if p in DISCOVERY_PLATFORMS]
    if not selected:
        selected = list(DISCOVERY_PLATFORMS)
    return [p for p in selected if platform_enabled(db, profile.user_id, p)]


def _acquire_platform_slot(user_id: int, platform: str) -> bool:
    """Per-(user, platform) pacing lock. Redis down → proceed (graceful)."""
    if cache._r is None:
        return True
    try:
        return bool(cache._r.set(f"discovery:{user_id}:{platform}", "1",
                                 nx=True, ex=DISCOVERY_LOCK_SECONDS))
    except Exception:  # noqa: BLE001 — pacing must never block discovery
        log.warning("discovery pacing lock unavailable; proceeding")
        return True


async def _search_platform(db: Session, user: User, platform: str,
                           terms: list[str]) -> list:
    """Run the platform adapter's search for each term; auth errors and
    adapter failures skip the platform (logged), never raise."""
    postings = []
    if platform == "freelancer":
        adapter = FreelancerAdapter(db, user.id)
    elif platform == "upwork":
        adapter = UpworkAgencyAdapter(db, user.id)
    else:
        import os
        adapter = LinkedInJobsAdapter(db, user_id=user.id,
                                      provider=os.getenv("LINKEDIN_PROVIDER", "theirstack"))
    try:
        for term in terms:
            postings.extend(await adapter.search_jobs(term, limit=25))
    except AdapterAuthError as exc:
        log.warning("discovery: skipping %s for user %d (auth): %s",
                    platform, user.id, exc)
        return []
    except AdapterError as exc:
        log.warning("discovery: %s search failed for user %d: %s",
                    platform, user.id, exc)
    finally:
        await adapter.close()
    return postings


async def run_profile_discovery(db: Session, user: User,
                                profile: SearchProfile,
                                respect_pacing: bool = True) -> dict:
    """Search the profile's platforms and ingest the results.

    Discovery + scoring/ingest run inline; proposal generation is enqueued
    by the ingest pipeline as Celery tasks. Manual "run now" calls pass
    respect_pacing=False (explicit user action beats the pacing lock).
    """
    terms = search_terms_for_profile(db, profile)
    searched, ingested = [], 0
    for platform in platforms_for_profile(db, profile):
        if respect_pacing and not _acquire_platform_slot(user.id, platform):
            log.info("discovery: %s for user %d skipped (paced)", platform, user.id)
            continue
        postings = await _search_platform(db, user, platform, terms)
        if not postings:
            continue
        result = await run_ingest(
            IngestJobsIn(jobs=[p.to_ingest() for p in postings]), db, user)
        ingested += result.ingested
        searched.append(platform)
    return {"queued": True, "platforms": searched, "ingested": ingested}
