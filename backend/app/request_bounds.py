"""Bound JSON work before untyped routes or credential enrollment consume it."""

import asyncio
import json
from starlette.responses import JSONResponse


class RequestBounds:
    def __init__(self, app, max_bytes=2_000_000):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in (
            "POST",
            "PUT",
            "PATCH",
        ):
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            try:
                message = await asyncio.wait_for(receive(), timeout=15)
            except asyncio.TimeoutError:
                return await JSONResponse({"detail": "request body timed out"}, 408)(
                    scope, receive, send
                )
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            size += len(part)
            if size > self.max_bytes:
                return await JSONResponse({"detail": "request body exceeds 2 MB"}, 413)(
                    scope, receive, send
                )
            chunks.append(part)
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        content_type = dict(scope.get("headers", [])).get(b"content-type", b"")
        if body and b"json" in content_type:
            try:

                def invalid_constant(value):
                    raise ValueError("non-finite number")

                data = json.loads(body, parse_constant=invalid_constant)
                pending = [(data, 0)]
                while pending:
                    value, depth = pending.pop()
                    if depth > 12:
                        raise ValueError("payload is too deeply nested")
                    if isinstance(value, str) and len(value) > 500000:
                        raise ValueError("text is too large")
                    if isinstance(value, (dict, list)):
                        if len(value) > 1000:
                            raise ValueError("collection exceeds 1000 entries")
                        pending.extend(
                            (child, depth + 1)
                            for child in (
                                value.values() if isinstance(value, dict) else value
                            )
                        )
            except (ValueError, RecursionError, UnicodeError):
                return await JSONResponse(
                    {"detail": "invalid or oversized JSON structure"}, 422
                )(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)
