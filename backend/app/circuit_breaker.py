"""Per-platform circuit breaker for automation tasks.

States: CLOSED (normal) → OPEN (halted, all automation skipped) → HALF_OPEN
(one trial task). Backed by Redis when available, in-process otherwise.
Opened manually (via API) or automatically after repeated stealth-task
failures / platform warnings.
"""
import logging
import time

from .cache import cache

log = logging.getLogger(__name__)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

_local: dict[str, dict] = {}
DEFAULT_COOLDOWN_SEC = 1800  # 30 min before half-open trial


def _key(platform: str) -> str:
    return f"circuit:{platform}"


def get_state(platform: str) -> dict:
    if cache._r is not None:
        data = cache.get_json(_key(platform))
        if data:
            return data
    return _local.get(platform, {"state": CLOSED, "opened_at": None, "reason": ""})


def is_closed(platform: str) -> bool:
    s = get_state(platform)
    if s["state"] == CLOSED:
        return True
    if s["state"] == OPEN:
        opened_at = s.get("opened_at") or 0
        if time.time() - opened_at > DEFAULT_COOLDOWN_SEC:
            transition(platform, HALF_OPEN, "cooldown elapsed, trial task allowed")
            return True
        return False
    return True  # half_open: allow a single trial


def transition(platform: str, state: str, reason: str = ""):
    data = {
        "state": state,
        "opened_at": time.time() if state == OPEN else None,
        "reason": reason,
    }
    _local[platform] = data
    if cache._r is not None:
        cache.set_json(_key(platform), data, ttl=86400)
    log.warning("circuit breaker %s → %s (%s)", platform, state, reason)


def open_circuit(platform: str, reason: str = ""):
    transition(platform, OPEN, reason)


def close_circuit(platform: str, reason: str = ""):
    transition(platform, CLOSED, reason)


def check(platform: str) -> tuple[bool, str]:
    """Gate for automation entry points. Returns (allowed, skip_reason)."""
    if is_closed(platform):
        return True, ""
    state = get_state(platform)
    return False, f"circuit OPEN for {platform}: {state.get('reason', 'no reason recorded')}"
