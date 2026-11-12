"""Per-tenant rate limiting in the router (docs/plan.md §4).

A token bucket per tenant: the bucket holds up to ``burst`` tokens and refills at
``rps`` tokens per second; each request takes one token. Requests over the limit are
rejected before they reach the cell.

With several router replicas, each enforces its share (``limit / replicas``), so the
replicas together stay within the tenant's limit without sharing state. A shared
Redis-backed limiter can replace this behind the same interface (stage 2c).
"""

import math
from dataclasses import dataclass
from typing import Protocol

from cellarch.common.clock import Clock, monotonic
from cellarch.common.contracts import RateLimit


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int  # this router's bucket size (X-RateLimit-Limit)
    remaining: int  # whole tokens left (X-RateLimit-Remaining)
    reset_after: int  # seconds until the bucket is full again (X-RateLimit-Reset)
    retry_after: int  # seconds until the next request can succeed (0 when allowed)


class RateLimiter(Protocol):
    def check(self, key: str, limit: RateLimit) -> Decision: ...


class InMemoryRateLimiter:
    def __init__(self, *, clock: Clock = monotonic, replicas: int = 1) -> None:
        if replicas < 1:
            raise ValueError("replicas must be at least 1")
        self._clock = clock
        self._replicas = replicas
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last update)

    def check(self, key: str, limit: RateLimit) -> Decision:
        rate = limit.rps / self._replicas
        capacity = max(1, limit.burst // self._replicas)
        now = self._clock()

        tokens, last = self._buckets.get(key, (float(capacity), now))
        tokens = min(float(capacity), tokens + (now - last) * rate)
        allowed = tokens >= 1
        if allowed:
            tokens -= 1
        self._buckets[key] = (tokens, now)

        return Decision(
            allowed=allowed,
            limit=capacity,
            remaining=math.floor(tokens),
            reset_after=math.ceil((capacity - tokens) / rate),
            retry_after=0 if allowed else max(1, math.ceil((1 - tokens) / rate)),
        )
