"""Manual-assist submission handlers: submit_proposal (PPH/Guru) and
submit_fiverr_offer.

Conservative by design: the handler navigates to the proposal/offer form,
fills it (human-typed, consuming the typing plan), and takes a screenshot —
but does NOT click the final submit button unless the allow-submit gate is
open for the task's platform (WORKER_ALLOW_SUBMIT_<PLATFORM>=1, falling back
to the global WORKER_ALLOW_SUBMIT=1). The result always says which happened
so the backend audit trail stays truthful.
"""
import logging

from ..browser import human_delay, raise_if_challenge, type_with_plan
from ..platforms import platform_config
from .base import HandlerContext, fetch_page

log = logging.getLogger(__name__)


def _manual_assist(task, ctx: HandlerContext, form_key: str,
                   submit_gate_selector: str) -> dict:
    payload = task.payload
    cfg = platform_config(task.platform)
    form = cfg[form_key]
    url = payload.get("url") or payload.get("job_url")
    if not url:
        raise RuntimeError(f"{task.task_type}: payload needs 'url'")

    page = fetch_page(ctx, task.platform, task.user_id, url)
    text = payload.get("humanized_text") or payload.get("proposal_text", "")
    message_selector = form.get("message")
    if not message_selector or page.query_selector(message_selector) is None:
        raise RuntimeError(
            f"{task.task_type}: message field not found on {url} — "
            f"selectors in worker/platforms.py likely drifted")
    type_with_plan(page, message_selector, text, payload.get("typing_plan") or [])
    human_delay(0.8, 1.8)
    raise_if_challenge(page, task.platform)

    shot = ctx.browser.screenshot(page, task.platform, task.user_id,
                                  f"task{task.id}-manual-assist")
    submitted = False
    attempted = ctx.config.allow_submit_for(task.platform)
    if attempted:
        # explicit operator opt-in: WORKER_ALLOW_SUBMIT[_<PLATFORM>]=1
        ctx.client.authorize_task(task.id)
        ctx.external_write_started = True
        page.click(form[submit_gate_selector])
        page.wait_for_load_state("domcontentloaded")
        human_delay(1.5, 3.0)
        raise_if_challenge(page, task.platform)
        # A successful click/load is not proof the platform accepted the offer.
        from .upwork_proposal import _verify_submission
        outcome = _verify_submission(page, cfg)
        submitted = outcome["submitted"]
        log.info("manual-assist task %d submission verdict: %r", task.id, submitted)
    else:
        log.info("manual-assist task %d filled, awaiting human final submit "
                 "(screenshot %s)", task.id, shot)
    return {
        "manual_assist": True,
        "filled": True,
        "submitted": submitted,
        **(outcome if attempted else {"state": "manual_action_required"}),
        "screenshot": shot,
        "note": (("Submission confirmed" if submitted is True else
                  "Check the platform before another submission; this attempt was not confirmed")
                 if attempted else
                 "Submit the reviewed text on the platform, then mark it submitted in GigHound"),
    }


def handle_submit_proposal(task, ctx: HandlerContext) -> dict:
    """Generic copy-assist platforms (PeoplePerHour, Guru, ...)."""
    return _manual_assist(task, ctx, "proposal_form", "submit_do_not_click")


def handle_submit_fiverr_offer(task, ctx: HandlerContext) -> dict:
    """Fiverr custom offer on a brief. Same manual-assist gate."""
    return _manual_assist(task, ctx, "offer_form", "submit_do_not_click")
