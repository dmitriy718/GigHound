"""scrape_proposal_status handler: READ-ONLY outcome/reply sync.

Loads the platform's proposals/inbox listing (Upwork proposals, Fiverr seller
inbox incl. brief responses, PeoplePerHour WorkStream, Guru quotes) and
extracts, for every proposal the backend asked about, a canonical status
(pending/viewed/interviewing/hired/declined) and an unread-message flag,
then posts the results to `POST /api/gigs/proposal-status`.

HITL: this handler NEVER clicks, types, or submits anything — it only reads
the page. Selectors are best-effort (see worker/platforms.py): none of these
listings has stable test ids, so matching falls back to the job URL /
external id contained in each card's links.
"""
import logging
import re

from ..platforms import platform_config
from .base import HandlerContext, extract_fields, fetch_page

log = logging.getLogger(__name__)

# canonical status buckets, first match wins (checked in order)
_STATUS_KEYWORDS = (
    ("hired", ("hired", "awarded", "won", "offer accepted", "contract")),
    ("declined", ("declined", "rejected", "archived", "withdrawn",
                  "not selected", "lost")),
    ("interviewing", ("interview", "shortlist", "messag", "active candidacy")),
    ("viewed", ("viewed", "seen")),
)


def canonical_status(raw: str) -> str:
    """Map a free-form status label to pending/viewed/interviewing/hired/declined."""
    text = (raw or "").lower()
    for canonical, keywords in _STATUS_KEYWORDS:
        if any(k in text for k in keywords):
            return canonical
    return "pending"


def _card_text(card) -> str:
    try:
        return card.inner_text() or ""
    except Exception:  # noqa: BLE001 — detached node mid-render
        return ""


def _card_job_ref(card) -> str:
    """All link hrefs on the card, joined (they hold the external id), or ''."""
    hrefs = []
    for link in card.query_selector_all("a[href]"):
        href = link.get_attribute("href")
        if href:
            hrefs.append(href)
    return " ".join(hrefs)


def _matches(item: dict, card, card_text: str, card_ref: str) -> bool:
    external_id = (item.get("job_external_id") or "").strip()
    job_url = (item.get("job_url") or "").strip()
    if external_id and (external_id in card_ref or external_id in card_text):
        return True
    if job_url and card_ref:
        tail = job_url.rstrip("/").split("/")[-1]
        if tail and tail in card_ref:
            return True
    return False


def handle_scrape_proposal_status(task, ctx: HandlerContext) -> dict:
    """Payload: {"items": [{proposal_queue_item_id, job_external_id, job_url}],
    "proposals_url"?: str}. Posts results to the backend and returns a summary.
    The platform comes from the task (upwork/fiverr/peopleperhour/guru)."""
    platform = getattr(task, "platform", None) or "upwork"
    cfg = platform_config(platform)
    items = task.payload.get("items", [])
    if not items:
        return {"checked": 0, "matched": 0}
    url = task.payload.get("proposals_url") or cfg["proposals_url"]

    page = fetch_page(ctx, platform, task.user_id, url)
    cards = page.query_selector_all(cfg.get("proposal_card", ""))
    field_map = cfg.get("proposal_card_fields", {})
    unread_sel = cfg.get("proposal_unread", "")

    results = []
    matched = 0
    for item in items:
        for card in cards:
            text = _card_text(card)
            ref = _card_job_ref(card)
            if not _matches(item, card, text, ref):
                continue
            fields = extract_fields(card, field_map)
            raw_status = fields.get("status") or ""
            if not raw_status:
                # no dedicated status node — scan the card text itself
                m = re.search(r"(hired|declined|interview\w*|viewed|pending)",
                              text, re.I)
                raw_status = m.group(1) if m else ""
            has_unread = bool(unread_sel and card.query_selector(unread_sel))
            results.append({
                "proposal_queue_item_id": item["proposal_queue_item_id"],
                "platform_status": canonical_status(raw_status),
                "has_unread_reply": has_unread,
            })
            matched += 1
            break
        else:
            log.info("proposal %s not found on the proposals page; skipped",
                     item.get("proposal_queue_item_id"))

    if results:
        ctx.client.post_proposal_status(task.id, results)
    log.info("proposal status scrape for user %s: %d/%d matched",
             task.user_id, matched, len(items))
    return {"checked": len(items), "matched": matched, "posted": len(results)}
