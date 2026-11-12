from __future__ import annotations

import pytest

from cellarch.common.contracts import RateLimit
from cellarch.data_plane.router.rate_limit import InMemoryRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def limiter(replicas: int = 1) -> tuple[InMemoryRateLimiter, FakeClock]:
    clock = FakeClock()
    return InMemoryRateLimiter(clock=clock, replicas=replicas), clock


def test_bucket_starts_full_and_counts_down() -> None:
    rl, _ = limiter()
    limit = RateLimit(rps=1, burst=3)
    decisions = [rl.check("acme", limit) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert [d.remaining for d in decisions] == [2, 1, 0, 0]
    assert all(d.limit == 3 for d in decisions)


def test_denied_requests_say_when_to_retry() -> None:
    rl, _ = limiter()
    limit = RateLimit(rps=0.5, burst=1)
    rl.check("acme", limit)
    denied = rl.check("acme", limit)
    assert not denied.allowed
    assert denied.retry_after == 2  # one token every 2 seconds


def test_retry_after_is_at_least_one_second() -> None:
    rl, clock = limiter()
    limit = RateLimit(rps=10, burst=1)
    rl.check("acme", limit)
    clock.now += 0.05  # half a token back
    denied = rl.check("acme", limit)
    assert not denied.allowed
    assert denied.retry_after == 1


def test_tokens_refill_but_never_beyond_the_burst() -> None:
    rl, clock = limiter()
    limit = RateLimit(rps=2, burst=2)
    rl.check("acme", limit)
    rl.check("acme", limit)
    clock.now += 0.5
    assert rl.check("acme", limit).allowed
    assert not rl.check("acme", limit).allowed
    clock.now += 100
    assert rl.check("acme", limit).remaining == 1  # capped at burst 2, minus this request


def test_reset_is_the_time_until_the_bucket_is_full() -> None:
    rl, _ = limiter()
    decision = rl.check("acme", RateLimit(rps=1, burst=5))
    assert decision.reset_after == 1
    decision = rl.check("acme", RateLimit(rps=1, burst=5))
    assert decision.reset_after == 2


def test_tenants_have_separate_buckets() -> None:
    rl, _ = limiter()
    limit = RateLimit(rps=0.001, burst=1)
    assert rl.check("acme", limit).allowed
    assert not rl.check("acme", limit).allowed
    assert rl.check("globex", limit).allowed


def test_each_replica_enforces_its_share() -> None:
    rl, _ = limiter(replicas=2)
    limit = RateLimit(rps=10, burst=4)
    decisions = [rl.check("acme", limit) for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[0].limit == 2


def test_share_never_drops_below_one_request() -> None:
    rl, _ = limiter(replicas=3)
    assert rl.check("acme", RateLimit(rps=1, burst=1)).allowed


def test_a_lower_limit_caps_saved_tokens() -> None:
    rl, _ = limiter()
    rl.check("acme", RateLimit(rps=0.001, burst=10))
    assert rl.check("acme", RateLimit(rps=0.001, burst=2)).remaining == 1


def test_replicas_must_be_positive() -> None:
    with pytest.raises(ValueError, match="replicas"):
        InMemoryRateLimiter(replicas=0)
