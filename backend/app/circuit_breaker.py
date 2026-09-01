"""Per-platform circuit breaker for automation tasks.

States: CLOSED (normal) → OPEN (halted, all automation skipped) → HALF_OPEN
(one trial task). Backed by Redis when available, in-process otherwise.
Opened manually (via API) or automatically after repeated stealth-task
failures / platform warnings.

Two scopes per platform: the platform-global key `circuit:{platform}`
(manual kill switch — blocks every tenant) and per-tenant keys
`circuit:{platform}:{user_id}` (auto-tripped by one tenant's failures — the
other tenants keep running). A per-tenant check honors BOTH.
"""
import logging
import time

from .cache import cache

log = logging.getLogger(__name__)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

_local: dict[str, dict] = {}
DEFAULT_COOLDOWN_SEC = 1800  # 30 min before half-open trial


def _key(platform: str, user_id: int | None = None) -> str:
    return f"circuit:{platform}" if user_id is None else f"circuit:{platform}:{user_id}"


def get_state(platform: str, user_id: int | None = None) -> dict:
    if cache._r is not None:
        data = cache.get_json(_key(platform, user_id))
        if data:
            return data
    return _local.get(_key(platform, user_id),
                      {"state": CLOSED, "opened_at": None, "reason": ""})


def is_closed(platform: str, user_id: int | None = None) -> bool:
    s = get_state(platform, user_id)
    if s["state"] == CLOSED:
        return True
    if s["state"] == OPEN:
        opened_at = s.get("opened_at") or 0
        if time.time() - opened_at > DEFAULT_COOLDOWN_SEC:
            transition(platform, HALF_OPEN, "cooldown elapsed, trial task allowed",
                       user_id=user_id)
            return True
        return False
    return True  # half_open: allow a single trial


def transition(platform: str, state: str, reason: str = "",
               user_id: int | None = None):
    data = {
        "state": state,
        "opened_at": time.time() if state == OPEN else None,
        "reason": reason,
    }
    _local[_key(platform, user_id)] = data
    if cache._r is not None:
        cache.set_json(_key(platform, user_id), data, ttl=86400)
    scope = platform if user_id is None else f"{platform} (user {user_id})"
    log.warning("circuit breaker %s → %s (%s)", scope, state, reason)


def open_circuit(platform: str, reason: str = "", user_id: int | None = None):
    transition(platform, OPEN, reason, user_id=user_id)


def close_circuit(platform: str, reason: str = "", user_id: int | None = None):
    transition(platform, CLOSED, reason, user_id=user_id)


def check(platform: str, user_id: int | None = None) -> tuple[bool, str]:
    """Gate for automation entry points. Returns (allowed, skip_reason).

    When user_id is given, BOTH scopes are honored: a platform-global open
    (manual kill switch) blocks every tenant, and a per-tenant open blocks
    just that tenant.
    """
    if not is_closed(platform):
        state = get_state(platform)
        return False, f"circuit OPEN for {platform}: {state.get('reason', 'no reason recorded')}"
    if user_id is not None and not is_closed(platform, user_id):
        state = get_state(platform, user_id)
        return False, (f"circuit OPEN for {platform} (user {user_id}): "
                       f"{state.get('reason', 'no reason recorded')}")
    return True, ""
