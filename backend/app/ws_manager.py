"""WebSocket fan-out for real-time alerts (AD-6).

`broadcast()` publishes to the Redis channel `gighound:ws:{user_id}` so
events reach every process (uvicorn workers, Celery tasks). A per-process
background subscriber (started on app startup, cancelled on shutdown)
forwards channel messages to that process's local connections. When Redis
is down, broadcast degrades to direct process-local delivery (dev/tests).
"""
import asyncio
import json
import logging

import redis.asyncio as aioredis
from fastapi import WebSocket

from .config import REDIS_URL

log = logging.getLogger(__name__)

WS_CHANNEL_PREFIX = "gighound:ws:"
_SUBSCRIBER_RETRY_SECONDS = 2


class AlertManager:
    """In-process WebSocket fan-out, bridged over Redis pub/sub.

    Connections are keyed by owning user id; broadcasts only reach the
    tenant that owns the resource the event is about (AD-1).
    """

    def __init__(self):
        self._connections: dict[int, set[WebSocket]] = {}
        self._redis: aioredis.Redis | None = None
        self._subscriber_task: asyncio.Task | None = None

    async def connect(self, ws: WebSocket, user_id: int):
        await ws.accept()
        self._connections.setdefault(user_id, set()).add(ws)

    def disconnect(self, ws: WebSocket, user_id: int):
        conns = self._connections.get(user_id)
        if conns is not None:
            conns.discard(ws)
            if not conns:
                self._connections.pop(user_id, None)

    async def broadcast(self, user_id: int, message: dict):
        """Publish an event for one tenant (all processes), with local fallback."""
        r = await self._get_redis()
        if r is not None:
            try:
                await r.publish(
                    f"{WS_CHANNEL_PREFIX}{user_id}",
                    json.dumps({"user_id": user_id, "message": message}, default=str),
                )
                return
            except Exception as exc:  # noqa: BLE001 — degrade to local
                log.warning("WS publish failed (%s); falling back to local", exc)
                self._redis = None
        await self._broadcast_local(user_id, message)

    async def _broadcast_local(self, user_id: int, message: dict):
        """Send a message to all of one user's live connections in THIS process."""
        conns = self._connections.get(user_id)
        if not conns:
            return
        payload = json.dumps(message, default=str)
        dead = []
        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, user_id)

    async def _get_redis(self) -> aioredis.Redis | None:
        if self._redis is not None:
            return self._redis
        try:
            client = aioredis.from_url(REDIS_URL, decode_responses=True)
            await client.ping()
            self._redis = client
        except Exception:  # noqa: BLE001 — Redis down: caller uses local path
            return None
        return self._redis

    # ---------------- pub/sub subscriber lifecycle ----------------

    async def start_subscriber(self):
        """Start the background fan-in task (called on app startup)."""
        if self._subscriber_task is None:
            self._subscriber_task = asyncio.create_task(self._subscribe_loop())

    async def stop_subscriber(self):
        """Cancel the subscriber and release Redis (called on app shutdown)."""
        task, self._subscriber_task = self._subscriber_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None

    async def _subscribe_loop(self):
        """Forward `gighound:ws:*` messages to local connections; resilient
        to Redis outages (retry loop) so the task never dies silently."""
        while True:
            client = None
            try:
                client = aioredis.from_url(REDIS_URL, decode_responses=True)
                async with client.pubsub() as pubsub:
                    await pubsub.psubscribe(f"{WS_CHANNEL_PREFIX}*")
                    async for raw in pubsub.listen():
                        if raw.get("type") not in ("pmessage", "message"):
                            continue
                        try:
                            data = json.loads(raw["data"])
                            await self._broadcast_local(int(data["user_id"]),
                                                        data["message"])
                        except Exception:  # noqa: BLE001
                            log.warning("dropping malformed WS fan-out message")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — Redis down; keep retrying
                log.warning("WS subscriber error (%s); retrying in %ds",
                            exc, _SUBSCRIBER_RETRY_SECONDS)
                await asyncio.sleep(_SUBSCRIBER_RETRY_SECONDS)
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass

    @property
    def connection_count(self) -> int:
        return sum(len(c) for c in self._connections.values())


alerts = AlertManager()
