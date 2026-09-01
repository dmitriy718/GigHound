"""create_gig_draft handler: fill the platform's gig form and save a DRAFT.

Hard rule (mirrors the backend's save_as_draft payload flag): the worker
NEVER publishes. The publish button selector lives in the platform config
only as a documented tripwire (publish_button_do_not_click) and is never
passed to page.click anywhere in this package.
"""
import logging

from ..browser import human_delay, raise_if_challenge, type_with_plan
from ..platforms import platform_config
from .base import HandlerContext, fetch_page

log = logging.getLogger(__name__)


def _fill(page, selector: str | None, value: str) -> bool:
    """Fill one field; True when the selector matched (and was filled)."""
    if not selector or not value:
        return False
    el = page.query_selector(selector)
    if el is None:
        return False
    el.fill(value)
    human_delay(0.3, 0.9)
    return True


def handle_create_gig_draft(task, ctx: HandlerContext) -> dict:
    payload = task.payload
    template = payload.get("template", {})
    cfg = platform_config(task.platform)
    form = cfg.get("gig_form")
    new_url = cfg.get("gig_new_url")
    if not form or not new_url:
        raise RuntimeError(
            f"no gig form config for '{task.platform}' — add gig_new_url/gig_form "
            "in worker/platforms.py")

    page = fetch_page(ctx, task.platform, task.user_id, new_url)

    # Track which expected fields actually filled: saving a half-empty draft
    # onto the tenant's real account when the form drifted is worse than
    # failing the task with an actionable missed-selector list.
    filled: list[str] = []
    missed: list[str] = []

    def attempt(field: str, selector: str | None, value: str):
        if not selector or not value:
            return  # nothing expected for this field — not a miss
        (filled if _fill(page, selector, value) else missed).append(field)

    attempt("title", form.get("title"), template.get("title", ""))
    attempt("category", form.get("category"),
            template.get("subcategory") or template.get("category", ""))
    tags = template.get("tags") or []
    if tags and form.get("tags"):
        if page.query_selector(form["tags"]) is not None:
            for tag in tags:
                tag_input = page.query_selector(form["tags"])
                tag_input.fill(tag)
                page.keyboard.press("Enter")
                human_delay(0.2, 0.6)
            filled.append("tags")
        else:
            missed.append("tags")
    attempt("price", form.get("price"),
            str(template.get("price") or template.get("basic_price") or ""))
    description = template.get("description", "")
    if description and form.get("description"):
        if page.query_selector(form["description"]) is not None:
            type_with_plan(page, form["description"], description,
                           payload.get("typing_plan") or [])
            filled.append("description")
        else:
            missed.append("description")
    human_delay(0.8, 1.8)
    raise_if_challenge(page, task.platform)

    # Require a majority of the expected fields, floor of 3 (the expected set
    # is title/category/tags/price/description — below that the draft is too
    # hollow to be worth saving on the tenant's account).
    expected = len(filled) + len(missed)
    required = min(expected, max(3, -(-expected // 2)))  # ceil(expected/2)
    if len(filled) < required:
        raise RuntimeError(
            f"gig form on {task.platform} looks drifted: filled "
            f"{len(filled)}/{expected} fields (need {required}) — "
            f"missed: {missed}; NOT saving a partial draft")

    page.click(form["save_draft"])  # DRAFT only — never the publish button
    page.wait_for_load_state("domcontentloaded")
    human_delay(1.0, 2.0)
    shot = ctx.browser.screenshot(page, task.platform, task.user_id,
                                  f"task{task.id}-gig-draft")
    log.info("gig draft saved for template %s on %s (never published)",
             payload.get("template_id"), task.platform)
    return {"draft": True, "published": False,
            "template_id": payload.get("template_id"), "screenshot": shot,
            "filled": filled, "missed": missed}
