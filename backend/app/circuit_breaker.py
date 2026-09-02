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

import redis

from .cache import cache

log = logging.getLogger(__name__)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

_local: dict[str, dict] = {}
DEFAULT_COOLDOWN_SEC = 1800  # 30 min before half-open trial
TRIAL_TOKEN_TTL = 60  # seconds; a crashed trial frees the slot after this

# in-process fallback trial tokens (key → acquired-at) — bounds trials per
# process when Redis is down; expires on the same TTL as the Redis token
_local_trials: dict[str, float] = {}


def _key(platform: str, user_id: int | None = None) -> str:
    return f"circuit:{platform}" if user_id is None else f"circuit:{platform}:{user_id}"


def _trial_key(platform: str, user_id: int | None = None) -> str:
    base = f"circuit:trial:{platform}"
    return base if user_id is None else f"{base}:{user_id}"


def _acquire_trial(platform: str, user_id: int | None = None) -> bool:
    """Half-open admission: ONE trial task at a time (Redis SET NX, else a
    per-process flag). Released by any transition out of half-open, or after
    TRIAL_TOKEN_TTL if the trial never reports back."""
    key = _trial_key(platform, user_id)
    if cache._r is not None:
        try:
            return bool(cache._r.set(key, "1", nx=True, ex=TRIAL_TOKEN_TTL))
        except redis.RedisError as exc:
            log.warning("Redis trial token failed (%s); using local bound", exc)
    acquired_at = _local_trials.get(key)
    if acquired_at is not None and time.time() - acquired_at < TRIAL_TOKEN_TTL:
        return False
    _local_trials[key] = time.time()
    return True


def _release_trial(platform: str, user_id: int | None = None):
    key = _trial_key(platform, user_id)
    _local_trials.pop(key, None)
    if cache._r is not None:
        try:
            cache._r.delete(key)
        except redis.RedisError as exc:
            log.warning("Redis trial token release failed (%s); TTL will free it", exc)


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
        else:
            return False
    # half_open: admit a single trial; concurrent checks are blocked until
    # the trial resolves (transition out) or its token TTL lapses
    return _acquire_trial(platform, user_id)


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
    if state != HALF_OPEN:
        # the trial resolved (success → CLOSED, failure → OPEN): free the slot
        _release_trial(platform, user_id)
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
