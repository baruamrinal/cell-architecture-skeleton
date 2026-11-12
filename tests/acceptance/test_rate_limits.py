"""Stage 2a acceptance tests: per-tenant rate limits (docs/acceptance-criteria.md A31-A37)."""

from __future__ import annotations

from .conftest import ROUTE_CACHE_TTL, Platform


def burst(platform: Platform, tenant_id: str, count: int) -> list[int]:
    return [platform.as_tenant(tenant_id, "GET", "/v1/items").status_code for _ in range(count)]


def test_a31_over_the_limit_gets_429_and_never_reaches_the_cell(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.set_limit("acme", rps=0.001, burst=2)
    assert burst(platform, "acme", 2) == [200, 200]
    seen_by_cell = len(platform.cells["cell-1"].requests)

    throttled = platform.as_tenant("acme", "GET", "/v1/items")

    assert (throttled.status_code, throttled.json()["error"]) == (429, "rate_limited")
    assert int(throttled.headers["Retry-After"]) >= 1
    assert len(platform.cells["cell-1"].requests) == seen_by_cell


def test_a32_one_throttled_tenant_does_not_affect_another(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.onboard("globex")  # same shared cell
    platform.set_limit("acme", rps=0.001, burst=1)

    assert burst(platform, "acme", 3) == [200, 429, 429]
    assert burst(platform, "globex", 3) == [200, 200, 200]


def test_a33_bursts_are_allowed_and_tokens_refill_over_time(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.set_limit("acme", rps=1, burst=2)

    assert burst(platform, "acme", 3) == [200, 200, 429]
    platform.clock.advance(1.0)
    assert burst(platform, "acme", 2) == [200, 429]


def test_a34_tier_defaults_overrides_and_clearing(platform: Platform) -> None:
    platform.register_cell("cell-1", capacity=10)
    standard = platform.create_tenant("acme").json()
    premium = platform.create_tenant("globex", tier="premium").json()

    overridden = platform.set_limit("acme", rps=2, burst=3)
    cleared = platform.admin.delete("/v1/tenants/acme/rate-limit")

    assert standard["rate_limit"] == {"rps": 50, "burst": 100, "source": "tier"}
    assert premium["rate_limit"] == {"rps": 500, "burst": 1000, "source": "tier"}
    assert overridden["rate_limit"] == {"rps": 2, "burst": 3, "source": "override"}
    assert overridden["updated_by"] == "admin"
    assert cleared.status_code == 200
    assert cleared.json()["rate_limit"] == {"rps": 50, "burst": 100, "source": "tier"}

    # The router applies a change once its cached route expires.
    platform.set_limit("acme", rps=0.001, burst=1)
    platform.clock.advance(ROUTE_CACHE_TTL + 1)
    assert burst(platform, "acme", 2) == [200, 429]


def test_a35_limits_still_apply_while_the_control_plane_is_down(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.set_limit("acme", rps=0.001, burst=2)
    assert burst(platform, "acme", 1) == [200]  # route (with its limit) is now cached

    platform.control_plane.reachable = False
    platform.clock.advance(ROUTE_CACHE_TTL * 2)

    assert burst(platform, "acme", 2) == [200, 429]


def test_a36_each_router_replica_enforces_its_share(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.set_limit("acme", rps=0.001, burst=4)

    with platform.extra_router(router_replicas=2) as replica:
        codes = [
            replica.get("/v1/items", headers={"X-Tenant-ID": "acme"}).status_code
            for _ in range(3)
        ]

    assert codes == [200, 200, 429]


def test_a37_responses_carry_rate_limit_headers(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.set_limit("acme", rps=1, burst=5)

    first = platform.as_tenant("acme", "GET", "/v1/items")
    second = platform.as_tenant("acme", "GET", "/v1/items")

    assert first.headers["X-RateLimit-Limit"] == "5"
    assert first.headers["X-RateLimit-Remaining"] == "4"
    assert second.headers["X-RateLimit-Remaining"] == "3"
    assert int(second.headers["X-RateLimit-Reset"]) >= 1
