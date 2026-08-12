"""Redis caching layer for search results and alert state.

Degrades gracefully to a no-op in-memory stub when Redis is unreachable,
so the API still boots for local development.
"""
import json
import logging

import redis

from .config import CACHE_TTL_SECONDS, REDIS_URL

log = logging.getLogger(__name__)


class _NullCache:
    def get_json(self, key):
        return None

    def set_json(self, key, value, ttl=CACHE_TTL_SECONDS):
        pass

    def delete(self, *keys):
        pass

    def invalidate_prefix(self, prefix):
        pass


class RedisCache:
    def __init__(self):
        try:
            self._r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            self._r.ping()
            log.info("Connected to Redis at %s", REDIS_URL)
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis unavailable (%s); cache disabled", exc)
            self._r = None

    def get_json(self, key):
        if not self._r:
            return None
        raw = self._r.get(key)
        return json.loads(raw) if raw else None

    def set_json(self, key, value, ttl=CACHE_TTL_SECONDS):
        if not self._r:
            return
        self._r.set(key, json.dumps(value, default=str), ex=ttl)

    def delete(self, *keys):
        if self._r:
            self._r.delete(*keys)

    def invalidate_prefix(self, prefix):
        if not self._r:
            return
        for key in self._r.scan_iter(f"{prefix}*"):
            self._r.delete(key)


cache = RedisCache()
