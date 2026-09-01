"""Main worker loop: poll → claim → dispatch → complete.

Pacing: one poll per configured platform every WORKER_POLL_INTERVAL_SEC
(default 45s) ± WORKER_POLL_JITTER_SEC (default 15s) — the backend owns the
per-platform action caps; the worker just stays polite.

Crash safety: every task ends in exactly one complete() call — success,
CAPTCHA escalation ({captcha: true} so the server-side circuit breaker trips
and a human is alerted), or failure with the error string.
"""
import argparse
import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import browser as _browser
from .browser import BrowserManager, CaptchaDetectedError, SessionExpiredError
from .client import BackendError, ClaimConflictError, WorkerClient
from .config import load_config
from .handlers import get_handler, LEGACY_ALIASES
from .handlers.base import HandlerContext, SelectorSuspectError

log = logging.getLogger(__name__)

# human-approved submissions run regardless of the circadian window: a human
# just clicked approve — delaying that is a product bug, not stealth.
SUBMIT_KINDS = frozenset({"submit_upwork_proposal", "submit_fiverr_offer",
                          "submit_proposal"})


def _may_run_now(task_type: str, config, hour: int | None = None) -> bool:
    """Circadian gate: outside WORKER_ACTIVE_HOURS (hours in config.timezone)
    scrape/fetch tasks stay queued — accounts active at 3:47am local every
    night are anomalous. Submit kinds always run (see SUBMIT_KINDS).
    Per-tenant timezone alignment is future work."""
    kind = LEGACY_ALIASES.get(task_type, task_type)
    if kind in SUBMIT_KINDS:
        return True
    window = config.active_hours_window()
    if window is None:
        return True
    lo, hi = window
    if hour is None:
        hour = datetime.now(ZoneInfo(config.timezone)).hour
    return lo <= hour < hi


def process_task(task, ctx: HandlerContext) -> None:
    """Claim one task, execute its handler, and report the outcome."""
    try:
        claimed = ctx.client.claim_task(task.id)
    except ClaimConflictError:
        log.info("task %d already claimed by another worker, skipping", task.id)
        return
    task = claimed  # server returns the authoritative payload

    handler = get_handler(task.task_type)
    if handler is None:
        ctx.client.complete_task(task.id, False,
                                 {"error": f"no handler for task_type '{task.task_type}'"})
        return
    _browser.arm_task_deadline(ctx.config.task_timeout_sec)
    try:
        result = handler(task, ctx)
    except _browser.TaskTimeoutError:
        log.error("task %d (%s) exceeded the %ss wall-clock budget",
                  task.id, task.task_type, ctx.config.task_timeout_sec)
        try:
            ctx.client.complete_task(task.id, False,
                                     {"error": "task timeout",
                                      "timeout_sec": ctx.config.task_timeout_sec})
        except (BackendError, ClaimConflictError, httpx.TransportError) as report_exc:
            log.error("could not report timeout for task %d: %s", task.id, report_exc)
    except CaptchaDetectedError as exc:
        log.warning("task %d hit a challenge on %s (%s) — escalating",
                    task.id, exc.platform, exc.marker)
        try:
            ctx.client.complete_task(task.id, False,
                                     {"captcha": True, "marker": exc.marker,
                                      "platform": exc.platform})
        except (BackendError, ClaimConflictError, httpx.TransportError) as report_exc:
            log.error("could not report captcha for task %d: %s", task.id, report_exc)
    except SessionExpiredError as exc:
        log.warning("task %d found a dead session on %s (%s) — the account "
                    "needs re-enrollment", task.id, exc.platform, exc.detail)
        try:
            ctx.client.complete_task(task.id, False,
                                     {"session_expired": True,
                                      "platform": exc.platform})
        except (BackendError, ClaimConflictError, httpx.TransportError) as report_exc:
            log.error("could not report session expiry for task %d: %s",
                      task.id, report_exc)
    except SelectorSuspectError as exc:
        log.warning("task %d extracted nothing on %s — selectors suspect: %s",
                    task.id, task.platform, exc)
        try:
            ctx.client.complete_task(task.id, False,
                                     {"selector_suspect": True,
                                      "error": str(exc)[:500]})
        except (BackendError, ClaimConflictError, httpx.TransportError) as report_exc:
            log.error("could not report selector drift for task %d: %s",
                      task.id, report_exc)
    except Exception as exc:  # noqa: BLE001 — crash-safe: report, don't die
        log.exception("task %d (%s) failed", task.id, task.task_type)
        try:
            ctx.client.complete_task(task.id, False,
                                     {"error": str(exc)[:500],
                                      "error_type": type(exc).__name__})
        except (BackendError, ClaimConflictError, httpx.TransportError) as report_exc:
            log.error("could not report failure for task %d: %s", task.id, report_exc)
    else:
        try:
            ctx.client.complete_task(task.id, True, result or {})
        except ClaimConflictError:
            # the handler already finalized the task server-side (e.g. the
            # proposal-status endpoint marks the task done) — benign
            log.info("task %d already finalized server-side, skipping completion",
                     task.id)
        except httpx.TransportError as exc:
            log.error("could not report completion for task %d: %s", task.id, exc)
    finally:
        _browser.disarm_task_deadline()
        # task hygiene: close the task's pages so leftover DOM state (open
        # modal, half-filled form from a crashed task) never leaks into the
        # next task on this tenant's context
        close_pages = getattr(ctx.browser, "close_pages", None)
        if close_pages is not None:
            try:
                close_pages(task.platform, task.user_id)
            except Exception as exc:  # noqa: BLE001 — never fail the report path
                log.warning("could not close pages after task %d: %s", task.id, exc)


def poll_once(ctx: HandlerContext) -> int:
    """One sweep over all configured platforms. Returns tasks executed."""
    ctx.browser.reap_idle_contexts()  # close contexts past the idle TTL
    executed = 0
    for platform in ctx.config.platforms:
        try:
            tasks = ctx.client.poll_tasks(platform)
        except (BackendError, ClaimConflictError, httpx.TransportError) as exc:
            log.error("poll failed for %s: %s", platform, exc)
            continue
        for task in tasks:
            if not _may_run_now(task.task_type, ctx.config):
                log.info("task %d (%s) outside active hours — leaving it queued",
                         task.id, task.task_type)
                continue
            try:
                process_task(task, ctx)
            except Exception:  # noqa: BLE001 — one bad task must not kill the sweep
                log.exception("task %d (%s) processing blew up", task.id, task.task_type)
            executed += 1
    return executed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GigHound stealth-browser worker")
    parser.add_argument("--once", action="store_true",
                        help="run a single poll sweep and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    log.info("worker %s starting (platforms=%s, api=%s, headless=%s)",
             config.worker_id, ",".join(config.platforms),
             config.api_url, config.headless)

    client = WorkerClient(config.api_url, config.worker_token, config.worker_id)
    try:
        with BrowserManager(config, client=client) as browser:
            ctx = HandlerContext(config=config, client=client, browser=browser)
            while True:
                executed = poll_once(ctx)
                if args.once:
                    log.info("--once: %d task(s) executed, exiting", executed)
                    return 0
                interval = config.poll_interval_sec + random.uniform(
                    -config.poll_jitter_sec, config.poll_jitter_sec)
                log.debug("sleeping %.0fs", interval)
                time.sleep(max(5.0, interval))
    except KeyboardInterrupt:
        log.info("shutting down")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
