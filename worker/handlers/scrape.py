"""Read-only scrape handlers: gig metrics and competitor snapshots.

Fully generic: navigate → challenge check → JSON-LD/selector extraction
driven by the per-platform config in worker/platforms.py.
"""
import logging

from ..platforms import platform_config
from .base import (HandlerContext, extract_cards, extract_fields, fetch_page,
                   parse_number, SelectorSuspectError, url_for)

log = logging.getLogger(__name__)


def handle_scrape_gig_metrics(task, ctx: HandlerContext) -> dict:
    """Scrape seller-dashboard metrics for each gig in the payload and post
    them to the backend. Payload gigs are {"id", "url", "title"} dicts;
    legacy int-only ids carry no URL and are reported as skipped."""
    cfg = platform_config(task.platform)
    scraped, skipped = [], []
    for gig in task.payload.get("gigs", []):
        if not isinstance(gig, dict) or not gig.get("url"):
            skipped.append({"gig": gig, "reason": "no url in payload"})
            continue
        page = fetch_page(ctx, task.platform, task.user_id, gig["url"])
        fields = extract_fields(page, cfg.get("metrics_fields", {}))
        if not fields:
            # every metric selector missed on a session-verified page — the
            # selectors drifted. Never post zeros: that would silently
            # corrupt the tenant's analytics with fabricated data.
            raise SelectorSuspectError(
                f"no metrics fields extracted for gig {gig['id']} on "
                f"{task.platform} — metrics_fields in worker/platforms.py "
                f"likely drifted")
        impressions = int(parse_number(fields.get("impressions")) or 0)
        clicks = int(parse_number(fields.get("clicks")) or 0)
        orders = int(parse_number(fields.get("orders")) or 0)
        revenue = parse_number(fields.get("revenue")) or 0.0
        ctx.client.post_metrics(gig["id"], impressions, clicks, orders, revenue)
        scraped.append({"gig_id": gig["id"], "impressions": impressions,
                        "clicks": clicks, "orders": orders, "revenue": revenue})
        log.info("metrics scraped for gig %s: %s", gig["id"], fields)
    return {"scraped": scraped, "skipped": skipped}


def handle_scrape_competitors(task, ctx: HandlerContext) -> dict:
    """Scrape a category/search page for the top competitor gigs and post a
    snapshot. Payload: {"category": str, "query"?: str, "my_price"?: float}.

    RESERVED/UNWIRED (P5-1): no backend producer enqueues this task type —
    kept for a future competitor-tracking config surface."""
    cfg = platform_config(task.platform)
    category = task.payload.get("category", "")
    query = task.payload.get("query") or category
    if task.payload.get("search_url"):
        url = task.payload["search_url"]
    elif query and "search_url" in cfg:
        url = url_for(ctx, task.platform, "search_url", query=query)
    else:
        url = url_for(ctx, task.platform, "category_url", category=category)

    page = fetch_page(ctx, task.platform, task.user_id, url)
    gigs = extract_cards(page, cfg.get("gig_card", ""),
                         cfg.get("gig_card_fields", {}), limit=10)
    for g in gigs:
        if "price" in g:
            g["price"] = parse_number(g["price"])
        if "rating" in g:
            g["rating"] = parse_number(g["rating"])
    resp = ctx.client.post_competitors(task.user_id, task.platform, category,
                                       gigs, my_price=task.payload.get("my_price"))
    log.info("competitor snapshot for %s/%s: %d gigs", task.platform, category, len(gigs))
    return {"category": category, "gigs_found": len(gigs), "snapshot_id": resp.get("id")}
