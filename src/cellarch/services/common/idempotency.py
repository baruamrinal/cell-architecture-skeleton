"""Idempotency keys for create requests (enterprise principle: stable APIs).

A client that retries a create with the same ``Idempotency-Key`` gets the original
response instead of a duplicate. Reusing a key for a different request is an error.
Only successful responses are stored, so a request that failed can be retried.
Keys expire after ``ttl_seconds`` (24 hours by default).
"""

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from cellarch.common.clock import Clock
from cellarch.common.errors import ApiError
from cellarch.common.sql import Database

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"
_KEY = re.compile(r"[\x21-\x7e]{1,255}")  # visible ASCII


class IdempotencyConflictError(Exception):
    """The key was already used for a different request."""


def request_fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyStore:
    def __init__(
        self,
        db: Database,
        namespace: str,
        *,
        ttl_seconds: float = 24 * 3600,
        clock: Clock = time.time,
    ) -> None:
        self._db = db
        self._table = f"{namespace}_idempotency_keys"
        self._ttl = ttl_seconds
        self._clock = clock
        with db.connect(lock=self._table) as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self._table} (key TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, status INTEGER NOT NULL, body TEXT NOT NULL, "
                "created_at DOUBLE PRECISION NOT NULL)"
            )

    def lookup(self, key: str, fingerprint: str) -> tuple[int, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {self._table} WHERE key = ?",  # noqa: S608
                (key,),
            ).fetchone()
            if row is None:
                return None
            if self._clock() - row["created_at"] > self._ttl:
                conn.execute(f"DELETE FROM {self._table} WHERE key = ?", (key,))  # noqa: S608
                return None
        if row["fingerprint"] != fingerprint:
            raise IdempotencyConflictError(key)
        return int(row["status"]), json.loads(row["body"])

    def save(self, key: str, fingerprint: str, status: int, body: Any) -> None:
        with self._db.connect() as conn:
            conn.execute(
                f"INSERT INTO {self._table} "  # noqa: S608
                "(key, fingerprint, status, body, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET fingerprint = excluded.fingerprint, "
                "status = excluded.status, body = excluded.body, "
                "created_at = excluded.created_at",
                (key, fingerprint, status, json.dumps(body), self._clock()),
            )


Handler = Callable[[], Awaitable[tuple[int, Any]]]


async def idempotent(
    store: IdempotencyStore, key: str | None, payload: Any, handler: Handler
) -> JSONResponse:
    """Run ``handler`` once per key; replay the stored response on retries."""
    if key is None:
        status, body = await handler()
        return JSONResponse(jsonable_encoder(body), status_code=status)
    if not _KEY.fullmatch(key):
        raise ApiError(
            400, "invalid_idempotency_key", f"{IDEMPOTENCY_HEADER} must be 1-255 visible characters"
        )

    fingerprint = request_fingerprint(payload)
    try:
        stored = store.lookup(key, fingerprint)
    except IdempotencyConflictError as exc:
        raise ApiError(
            422, "idempotency_key_reused", "this key was already used for a different request"
        ) from exc
    if stored is not None:
        status, body = stored
        return JSONResponse(body, status_code=status, headers={REPLAY_HEADER: "true"})

    status, result = await handler()
    body = jsonable_encoder(result)
    if 200 <= status < 300:
        store.save(key, fingerprint, status, body)
    return JSONResponse(body, status_code=status)
