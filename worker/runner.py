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

import httpx

from .browser import BrowserManager, CaptchaDetectedError, SessionExpiredError
from .client import BackendError, ClaimConflictError, WorkerClient
from .config import load_config
from .handlers import get_handler
from .handlers.base import HandlerContext, SelectorSuspectError

log = logging.getLogger(__name__)


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
    try:
        result = handler(task, ctx)
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
