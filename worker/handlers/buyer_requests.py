"""fetch_buyer_requests handler: scrape the platform's buyer-request surface
(Fiverr Briefs — "Buyer Requests" was retired in 2022) and post the raw
requests to the backend, which filters/matches/queues them for HITL review.
"""
import logging

from ..platforms import platform_config
from .base import HandlerContext, extract_cards, fetch_page, parse_number, url_for

log = logging.getLogger(__name__)


def handle_fetch_buyer_requests(task, ctx: HandlerContext) -> dict:
    cfg = platform_config(task.platform)
    if task.payload.get("briefs_url"):
        url = task.payload["briefs_url"]
    else:
        username = task.payload.get("username", "me")
        url = url_for(ctx, task.platform, "briefs_url", username=username)

    page = fetch_page(ctx, task.platform, task.user_id, url)
    rows = extract_cards(page, cfg.get("brief_card", ""),
                         cfg.get("brief_fields", {}), limit=25)
    requests = []
    for i, row in enumerate(rows):
        requests.append({
            "id": row.get("url") or f"brief-{i}",
            "title": row.get("title", ""),
            "description": row.get("description", ""),
            "budget": parse_number(row.get("budget")),
            "url": row.get("url", ""),
            "category": task.payload.get("category", ""),
            "currency": "USD",
        })
    resp = ctx.client.post_buyer_requests(task.user_id, requests)
    log.info("buyer requests for user %s: %d fetched, %s queued",
             task.user_id, len(requests), resp.get("queued"))
    return {"fetched": len(requests), "queued": resp.get("queued", 0)}
