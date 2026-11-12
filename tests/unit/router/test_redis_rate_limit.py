from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import pytest

from cellarch.common.contracts import RateLimit
from cellarch.data_plane.router.app import RouterSettings
from cellarch.data_plane.router.rate_limit import (
    InMemoryRateLimiter,
    RedisRateLimiter,
    build_rate_limiter,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class FakeScript:
    """Stands in for the Lua script: returns [allowed, tokens left] or raises."""

    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[tuple[list[str], list[Any]]] = []

    def __call__(self, keys: Sequence[str], args: Sequence[Any]) -> Any:
        self.calls.append((list(keys), list(args)))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make(results: list[Any], clock: FakeClock | None = None) -> tuple[RedisRateLimiter, FakeScript]:
    clock = clock or FakeClock()
    script = FakeScript(results)
    limiter = RedisRateLimiter(
        script,
        fallback=InMemoryRateLimiter(clock=clock, replicas=2),
        clock=clock,
        key_prefix="cellarch:rl:",
        cooldown_seconds=5,
    )
    return limiter, script


LIMIT = RateLimit(rps=1, burst=4)


def test_uses_the_full_limit_and_a_prefixed_key() -> None:
    limiter, script = make([[1, "3"]])
    decision = limiter.check("acme", LIMIT)
    assert script.calls == [(["cellarch:rl:acme"], [1.0, 4])]
    assert (decision.allowed, decision.limit, decision.remaining) == (True, 4, 3)
    assert decision.reset_after == 1


def test_denied_by_redis() -> None:
    limiter, _ = make([[0, "0.25"]])
    decision = limiter.check("acme", LIMIT)
    assert not decision.allowed
    assert decision.retry_after == 1  # ceil(0.75 / 1)
    assert decision.remaining == 0


def test_redis_errors_fall_back_and_open_the_circuit(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    limiter, script = make([ConnectionError("down"), [1, "3"]], clock)

    with caplog.at_level(logging.WARNING):
        first = limiter.check("acme", LIMIT)
        second = limiter.check("acme", LIMIT)

    # In-memory fallback with the per-replica share (burst 4 / 2 replicas = 2).
    assert (first.allowed, first.limit) == (True, 2)
    assert second.allowed
    assert len(script.calls) == 1  # circuit open: Redis not tried again yet
    assert sum("falling back" in r.getMessage() for r in caplog.records) == 1

    clock.now += 5
    assert limiter.check("acme", LIMIT).limit == 4  # Redis tried again, and answered
    assert len(script.calls) == 2


def test_build_memory_backend() -> None:
    settings = RouterSettings(control_plane_url="http://cp", router_token="r")
    assert isinstance(build_rate_limiter(settings, FakeClock()), InMemoryRateLimiter)


def test_build_redis_backend_without_connecting() -> None:
    settings = RouterSettings(
        control_plane_url="http://cp",
        router_token="r",
        rate_limit_backend="redis",
        redis_url="redis://127.0.0.1:1/0",
    )
    assert isinstance(build_rate_limiter(settings, FakeClock()), RedisRateLimiter)


@pytest.mark.parametrize(
    ("backend", "url", "message"),
    [("redis", None, "REDIS_URL"), ("memcached", None, "RATE_LIMIT_BACKEND")],
)
def test_build_rejects_bad_configuration(backend: str, url: str | None, message: str) -> None:
    settings = RouterSettings(
        control_plane_url="http://cp", router_token="r", rate_limit_backend=backend, redis_url=url
    )
    with pytest.raises(ValueError, match=message):
        build_rate_limiter(settings, FakeClock())


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp")
    monkeypatch.setenv("ROUTER_TOKEN", "r")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    settings = RouterSettings.from_env()
    assert (settings.rate_limit_backend, settings.redis_url) == ("redis", "redis://redis:6379/0")
