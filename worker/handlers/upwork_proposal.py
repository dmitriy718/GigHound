"""submit_upwork_proposal handler: agency Business Manager submission.

The backend only creates this task for APPROVED review-queue items (HITL is
enforced server-side — see routers/proposals.py / adapters.py); this handler
performs the actual submission through the agency manager's stored browser
session and escalates to a human on any challenge (CAPTCHA, re-auth, 2FA).
"""
import logging

from ..browser import human_delay, mouse_wiggle, raise_if_challenge, type_with_plan
from ..platforms import platform_config
from .base import HandlerContext, fetch_page

log = logging.getLogger(__name__)


def _select_by_label(page, selector: str, label: str):
    """Select a dropdown option by visible label when the control exists."""
    el = page.query_selector(selector)
    if el is not None:
        el.select_option(label=label)


def handle_submit_upwork_proposal(task, ctx: HandlerContext) -> dict:
    payload = task.payload
    cfg = platform_config("upwork")
    form = cfg["proposal_form"]
    url = payload.get("job_url") or cfg["job_url"].format(
        external_id=payload.get("job_external_id", ""))

    page = fetch_page(ctx, "upwork", task.user_id, url)
    mouse_wiggle(page)

    page.click(form["apply_button"])
    page.wait_for_selector(form["cover_letter"], timeout=15000)
    human_delay(0.8, 1.8)
    raise_if_challenge(page, "upwork")  # challenges often appear mid-flow

    on_behalf_of = payload.get("on_behalf_of")
    if on_behalf_of:
        _select_by_label(page, form["agency_selector"], on_behalf_of)
        _select_by_label(page, form["member_selector"], on_behalf_of)
        human_delay()

    if payload.get("bid_amount"):
        bid = page.query_selector(form["bid_amount"])
        if bid is not None:
            bid.fill(str(payload["bid_amount"]))

    text = payload.get("humanized_text") or payload.get("proposal_text", "")
    type_with_plan(page, form["cover_letter"], text,
                   payload.get("typing_plan") or [])
    human_delay(1.0, 2.5)

    shot_before = ctx.browser.screenshot(page, "upwork", task.user_id,
                                         f"task{task.id}-before-submit")
    # Submission IS the approved action here (queue item was human-approved);
    # the WORKER_ALLOW_SUBMIT gate applies to manual-assist platforms only.
    page.click(form["submit"])
    page.wait_for_load_state("domcontentloaded")
    human_delay(1.5, 3.0)
    raise_if_challenge(page, "upwork")
    shot_after = ctx.browser.screenshot(page, "upwork", task.user_id,
                                        f"task{task.id}-after-submit")
    log.info("upwork proposal submitted for job %s on behalf of %s",
             payload.get("job_external_id"), on_behalf_of)
    return {
        "submitted": True,
        "job_external_id": payload.get("job_external_id", ""),
        "on_behalf_of": on_behalf_of,
        "screenshots": [shot_before, shot_after],
    }
