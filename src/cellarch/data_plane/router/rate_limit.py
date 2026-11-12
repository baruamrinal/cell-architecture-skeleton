"""Per-tenant rate limiting in the router (docs/plan.md §4).

A token bucket per tenant: the bucket holds up to ``burst`` tokens and refills at
``rps`` tokens per second; each request takes one token. Requests over the limit are
rejected before they reach the cell.

Two backends behind one interface:
  * in memory (default): with several router replicas each enforces its share
    (``limit / replicas``), so together they stay within the limit without shared state.
  * Redis (``RATE_LIMIT_BACKEND=redis``): all replicas share one bucket per tenant, so
    the full limit is exact. It fails open: if Redis is unreachable the router uses its
    in-memory limiter, and a circuit breaker skips Redis for a few seconds so an outage
    costs one slow request, not every request.
"""

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from cellarch.common.clock import Clock, monotonic
from cellarch.common.contracts import RateLimit

if TYPE_CHECKING:  # pragma: no cover
    from cellarch.data_plane.router.app import RouterSettings

log = logging.getLogger("cellarch.router")


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


def _decision(allowed: bool, tokens: float, rate: float, capacity: int) -> Decision:
    return Decision(
        allowed=allowed,
        limit=capacity,
        remaining=math.floor(tokens),
        reset_after=math.ceil((capacity - tokens) / rate),
        retry_after=0 if allowed else max(1, math.ceil((1 - tokens) / rate)),
    )


# Atomic token bucket in Redis. Uses Redis's own clock, so every router agrees on time.
# KEYS[1] = bucket key; ARGV = rate (tokens/second), capacity. Returns {allowed, tokens}.
TOKEN_BUCKET_LUA = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
return {allowed, tostring(tokens)}
"""  # noqa: S105 - a Lua script, not a secret

Script = Callable[[Sequence[str], Sequence[Any]], Any]


class RedisRateLimiter:
    def __init__(
        self,
        script: Script,
        *,
        fallback: RateLimiter,
        clock: Clock = monotonic,
        key_prefix: str = "cellarch:rl:",
        cooldown_seconds: float = 5.0,
    ) -> None:
        self._script = script
        self._fallback = fallback
        self._clock = clock
        self._prefix = key_prefix
        self._cooldown = cooldown_seconds
        self._open_until = float("-inf")  # circuit breaker: skip Redis until then

    def check(self, key: str, limit: RateLimit) -> Decision:
        now = self._clock()
        if now < self._open_until:
            return self._fallback.check(key, limit)
        try:
            allowed, tokens = self._script([f"{self._prefix}{key}"], [limit.rps, limit.burst])
        except Exception as exc:  # fail open on any Redis problem: traffic must keep flowing
            log.warning(
                "Redis rate limiter unavailable (%s); falling back to in-memory limits for %ss",
                exc,
                self._cooldown,
            )
            self._open_until = now + self._cooldown
            return self._fallback.check(key, limit)
        return _decision(bool(int(allowed)), float(tokens), limit.rps, limit.burst)


def build_rate_limiter(settings: "RouterSettings", clock: Clock) -> RateLimiter:
    """The limiter selected by RATE_LIMIT_BACKEND; fails fast on bad configuration."""
    in_memory = InMemoryRateLimiter(clock=clock, replicas=settings.router_replicas)
    if settings.rate_limit_backend == "memory":
        return in_memory
    if settings.rate_limit_backend != "redis":
        raise ValueError(
            f"RATE_LIMIT_BACKEND must be 'memory' or 'redis', not {settings.rate_limit_backend!r}"
        )
    if not settings.redis_url:
        raise ValueError("RATE_LIMIT_BACKEND=redis needs REDIS_URL")

    import redis  # only needed when the Redis backend is switched on

    client = redis.Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
    )
    lua = client.register_script(TOKEN_BUCKET_LUA)
    return RedisRateLimiter(
        lambda keys, args: lua(keys=list(keys), args=list(args)),
        fallback=in_memory,
        clock=clock,
        key_prefix=settings.rate_limit_key_prefix,
    )
