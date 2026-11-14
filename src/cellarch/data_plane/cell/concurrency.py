"""Cap on requests a cell handles at once: the backstop behind the router's rate limits.

Over the cap, new requests are rejected immediately with ``503 cell_overloaded`` instead
of queueing, so a spike can't pile up and take the cell down. Health checks are exempt,
so a busy cell is never mistaken for a dead one.
"""

import json

from starlette.types import ASGIApp, Receive, Scope, Send

from cellarch.common.errors import GRAPHQL_PATH
from cellarch.common.headers import CELL_HEADER


class ConcurrencyLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_concurrent: int,
        cell_id: str,
        exempt: tuple[str, ...] = ("/healthz",),
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.app = app
        self.max_concurrent = max_concurrent
        self.cell_id = cell_id
        self.exempt = exempt
        self.in_flight = 0  # single event loop: no lock needed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        if self.in_flight >= self.max_concurrent:
            await self._reject(send, scope["path"])
            return
        self.in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1

    async def _reject(self, send: Send, path: str) -> None:
        message = f"{self.cell_id} is at capacity; retry"
        error: dict[str, object] = {"error": "cell_overloaded", "message": message}
        if path == GRAPHQL_PATH:  # the web UI gets the same error as a GraphQL error
            extensions = {"code": "cell_overloaded", "status": 503, "retryAfter": 1}
            error = {"errors": [{"message": message, "extensions": extensions}], "data": None}
        body = json.dumps(error).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", b"1"),
                    (CELL_HEADER.lower().encode(), self.cell_id.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
