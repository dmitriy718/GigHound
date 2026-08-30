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


def _fill(page, selector: str | None, value: str):
    if not selector or not value:
        return
    el = page.query_selector(selector)
    if el is not None:
        el.fill(value)
        human_delay(0.3, 0.9)


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
    _fill(page, form.get("title"), template.get("title", ""))
    _fill(page, form.get("category"), template.get("subcategory") or template.get("category", ""))
    for tag in template.get("tags") or []:
        tag_input = page.query_selector(form.get("tags", ""))
        if tag_input is not None:
            tag_input.fill(tag)
            page.keyboard.press("Enter")
            human_delay(0.2, 0.6)
    _fill(page, form.get("price"),
          str(template.get("price") or template.get("basic_price") or ""))
    description = template.get("description", "")
    if description and form.get("description"):
        type_with_plan(page, form["description"], description,
                       payload.get("typing_plan") or [])
    human_delay(0.8, 1.8)
    raise_if_challenge(page, task.platform)

    page.click(form["save_draft"])  # DRAFT only — never the publish button
    page.wait_for_load_state("domcontentloaded")
    human_delay(1.0, 2.0)
    shot = ctx.browser.screenshot(page, task.platform, task.user_id,
                                  f"task{task.id}-gig-draft")
    log.info("gig draft saved for template %s on %s (never published)",
             payload.get("template_id"), task.platform)
    return {"draft": True, "published": False,
            "template_id": payload.get("template_id"), "screenshot": shot}
