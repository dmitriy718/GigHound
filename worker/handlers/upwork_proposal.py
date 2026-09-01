"""submit_upwork_proposal handler: agency Business Manager submission.

The backend only creates this task for APPROVED review-queue items (HITL is
enforced server-side — see routers/proposals.py / adapters.py); this handler
performs the actual submission through the agency manager's stored browser
session and escalates to a human on any challenge (CAPTCHA, re-auth, 2FA).
"""
import logging

from ..browser import (CaptchaDetectedError, human_delay, mouse_wiggle,
                       raise_if_challenge, type_with_plan)
from ..platforms import platform_config
from .base import HandlerContext, fetch_page

log = logging.getLogger(__name__)

# short window for the SPA to render a submit outcome toast/banner
_SUBMIT_VERIFY_TIMEOUT_MS = 8000


def _select_by_label(page, selector: str, label: str):
    """Select a dropdown option by visible label when the control exists."""
    el = page.query_selector(selector)
    if el is not None:
        el.select_option(label=label)


def _first_marker(page, markers: list[str]) -> str | None:
    for marker in markers:
        try:
            if page.query_selector(marker):
                return marker
        except Exception as exc:  # noqa: BLE001 — selector engines may reject a marker
            log.debug("submit marker %r check failed: %s", marker, exc)
    return None


def _verify_submission(page, cfg: dict) -> dict:
    """Confirm the submit click's outcome against the platform's markers
    (best-effort — see worker/platforms.py). Never guess: confirmed success,
    confirmed rejection (with reason), or unverified — the click already
    happened, so an unverified outcome must NOT fail the task (that would
    invite a duplicate submit on re-approval)."""
    success_markers = cfg.get("submit_success", [])
    if success_markers:
        try:
            page.wait_for_selector(success_markers[0],
                                   timeout=_SUBMIT_VERIFY_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — absence within the window is data
            pass
    marker = _first_marker(page, success_markers)
    if marker:
        return {"submitted": True, "confirm_marker": marker}
    marker = _first_marker(page, cfg.get("submit_failure", []))
    if marker:
        return {"submitted": False,
                "reason": f"platform rejected the submit (matched {marker!r})"}
    return {"submitted": None, "state": "submitted_unverified",
            "reason": "no success/failure marker matched after the submit click"}


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
    try:
        page.wait_for_load_state("domcontentloaded")
        human_delay(1.5, 3.0)
        raise_if_challenge(page, "upwork")
        outcome = _verify_submission(page, cfg)
    except CaptchaDetectedError:
        raise  # human escalation still wins over the duplicate-submit risk
    except Exception as exc:  # noqa: BLE001 — the click already happened:
        # failing the task invites a duplicate submit on re-approval, so any
        # post-click error degrades to "unverified" for a human to check
        log.warning("task %d post-submit verification blew up (%s) — "
                    "reporting submitted_unverified", task.id, exc)
        outcome = {"submitted": None, "state": "submitted_unverified",
                   "reason": f"post-submit verification failed: {exc}"}
    shots = [shot_before]
    try:
        shots.append(ctx.browser.screenshot(page, "upwork", task.user_id,
                                            f"task{task.id}-after-submit"))
    except Exception as exc:  # noqa: BLE001 — same duplicate-submit reasoning
        log.warning("task %d post-submit screenshot failed: %s", task.id, exc)
    if outcome.get("submitted"):
        log.info("upwork proposal submitted for job %s on behalf of %s",
                 payload.get("job_external_id"), on_behalf_of)
    else:
        log.warning("upwork proposal for job %s NOT confirmed: %s",
                    payload.get("job_external_id"), outcome.get("reason"))
    return {
        "job_external_id": payload.get("job_external_id", ""),
        "on_behalf_of": on_behalf_of,
        "screenshots": shots,
        **outcome,
    }
