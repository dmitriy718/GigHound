"""Redis caching layer for search results and alert state.

Degrades gracefully to a no-op in-memory stub when Redis is unreachable,
so the API still boots for local development. Runtime failures (Redis
drops after boot) are swallowed per operation with a warning, and the
client is recreated lazily so a recovering Redis is picked up without a
process restart.
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
        self._r = None
        try:
            self._connect()
            log.info("Connected to Redis at %s", REDIS_URL)
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis unavailable (%s); cache disabled "
                        "(reconnect retried on each operation)", exc)

    def _connect(self):
        """(Re)create the client. Called lazily from every operation when the
        client is None, so a Redis that comes back is picked up without a
        process restart. Raises on failure — callers guard with try."""
        self._r = redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                       socket_timeout=2,
                                       socket_connect_timeout=2)
        self._r.ping()

    def _client(self):
        """Live client or None: reconnect attempt when down, drop the client
        on runtime errors so the next operation retries the connection."""
        if self._r is None:
            try:
                self._connect()
                log.info("Reconnected to Redis at %s", REDIS_URL)
            except Exception:  # noqa: BLE001 — still down; stay degraded
                self._r = None
        return self._r

    def get_json(self, key):
        r = self._client()
        if r is None:
            return None
        try:
            raw = r.get(key)
        except redis.RedisError as exc:
            self._r = None
            log.warning("Redis get failed (%s); treating as cache miss", exc)
            return None
        return json.loads(raw) if raw else None

    def set_json(self, key, value, ttl=CACHE_TTL_SECONDS):
        r = self._client()
        if r is None:
            return
        try:
            r.set(key, json.dumps(value, default=str), ex=ttl)
        except redis.RedisError as exc:
            self._r = None
            log.warning("Redis set failed (%s); cache write skipped", exc)

    def delete(self, *keys):
        r = self._client()
        if r is None:
            return
        try:
            r.delete(*keys)
        except redis.RedisError as exc:
            self._r = None
            log.warning("Redis delete failed (%s); stale entries left", exc)

    def invalidate_prefix(self, prefix):
        r = self._client()
        if r is None:
            return
        try:
            for key in r.scan_iter(f"{prefix}*"):
                r.delete(key)
        except redis.RedisError as exc:
            self._r = None
            log.warning("Redis invalidate failed (%s); prefix %r left stale",
                        exc, prefix)


cache = RedisCache()
