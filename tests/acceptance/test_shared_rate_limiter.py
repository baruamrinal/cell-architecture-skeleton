"""Stage 2c acceptance tests: the shared Redis rate limiter (A40-A41)."""

from __future__ import annotations

import os
import uuid

import pytest

from .conftest import Platform

# Nothing listens here: connecting fails at once (fail-open path).
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


def codes(client: object, tenant_id: str, count: int) -> list[int]:
    return [
        client.get("/v1/items", headers={"X-Tenant-ID": tenant_id}).status_code  # type: ignore[attr-defined]
        for _ in range(count)
    ]


def test_a40_redis_down_falls_back_to_in_memory_limits(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.set_limit("acme", rps=0.001, burst=2)

    with platform.extra_router(
        rate_limit_backend="redis", redis_url=UNREACHABLE_REDIS
    ) as router:
        assert codes(router, "acme", 3) == [200, 200, 429]


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL not set: no Redis available for the shared-limiter test")
    import redis

    try:
        redis.Redis.from_url(url, socket_connect_timeout=1).ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis at {url} unreachable: {exc}")
    return url


def test_a41_routers_sharing_redis_share_one_bucket(platform: Platform, redis_url: str) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.set_limit("acme", rps=0.001, burst=4)
    shared = {
        "rate_limit_backend": "redis",
        "redis_url": redis_url,
        "rate_limit_key_prefix": f"test:{uuid.uuid4().hex}:",  # isolate from other runs
        "router_replicas": 2,  # ignored with Redis: the bucket is shared, not split
    }

    with platform.extra_router(**shared) as first, platform.extra_router(**shared) as second:
        assert codes(first, "acme", 2) == [200, 200]
        assert codes(second, "acme", 3) == [200, 200, 429]
