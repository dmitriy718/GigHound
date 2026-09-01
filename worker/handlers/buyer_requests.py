"""fetch_buyer_requests handler: scrape the platform's buyer-request surface
(Fiverr Briefs — "Buyer Requests" was retired in 2022) and post the raw
requests to the backend, which filters/matches/queues them for HITL review.
"""
import hashlib
import logging
from urllib.parse import urljoin, urlparse

from ..platforms import platform_config
from .base import (HandlerContext, extract_cards, fetch_page, parse_currency,
                   parse_number, url_for)

log = logging.getLogger(__name__)


def _external_id(url: str, row: dict) -> str:
    """Stable per-brief id (backend dedups on it): the brief URL path when the
    card links somewhere, else a content hash — never the scrape index, which
    changes on every poll and would defeat dedup."""
    path = urlparse(url).path.strip("/")
    if path:
        return path
    digest = hashlib.sha1("|".join([
        row.get("title", ""), row.get("budget", ""), row.get("description", ""),
    ]).encode()).hexdigest()
    return f"brief-{digest[:16]}"


def handle_fetch_buyer_requests(task, ctx: HandlerContext) -> dict:
    cfg = platform_config(task.platform)
    if task.payload.get("briefs_url"):
        url = task.payload["briefs_url"]
    else:
        username = task.payload.get("username")
        if not username:
            raise ValueError(
                "no seller username configured: the briefs page lives at "
                "/users/{username}/briefs — set 'username' in the fiverr "
                "account settings (backend puts it in the task payload)")
        url = url_for(ctx, task.platform, "briefs_url", username=username)

    page = fetch_page(ctx, task.platform, task.user_id, url)
    rows = extract_cards(page, cfg.get("brief_card", ""),
                         cfg.get("brief_fields", {}), limit=25)
    requests = []
    for row in rows:
        href = row.get("url") or ""
        brief_url = urljoin(cfg["base_url"] + "/", href) if href else ""
        budget_text = row.get("budget")
        requests.append({
            "id": _external_id(brief_url, row),
            "title": row.get("title", ""),
            "description": row.get("description", ""),
            "budget": parse_number(budget_text),
            "url": brief_url,
            "category": task.payload.get("category", ""),
            # parsed from the budget text; None when undetectable — the
            # backend applies its own default, we never invent one
            "currency": parse_currency(budget_text),
        })
    resp = ctx.client.post_buyer_requests(task.user_id, requests)
    log.info("buyer requests for user %s: %d fetched, %s queued",
             task.user_id, len(requests), resp.get("queued"))
    return {"fetched": len(requests), "queued": resp.get("queued", 0)}
