"""Stage 1 acceptance tests: the core platform (docs/plan.md §13, stage 1).

Written before the implementation; the stage is done when these pass.
"""

from __future__ import annotations

import logging

import pytest

from cellarch.common.headers import CELL_HEADER, INTERNAL_TOKEN_HEADER, TENANT_HEADER

from .conftest import ADMIN_TOKEN, CELL_TOKENS, ROUTE_CACHE_TTL, ROUTER_TOKEN, Platform


def test_a1_onboarding_places_tenant_on_a_shared_cell_and_provisions_it(
    platform: Platform,
) -> None:
    platform.register_cell("cell-1")

    assert platform.onboard("acme") == "cell-1"

    tenant = platform.admin.get("/v1/tenants/acme").json()
    assert tenant["cell_id"] == "cell-1"
    assert tenant["status"] == "active"
    assert tenant["placement_mode"] == "pooled"
    direct = platform.cell_clients["cell-1"].get("/v1/users", headers={TENANT_HEADER: "acme"})
    assert direct.status_code == 200


def test_a2_router_sends_tenant_requests_to_its_cell(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.register_cell("cell-2")
    cell_id = platform.onboard("acme")

    created = platform.as_tenant(
        "acme", "POST", "/v1/users", json={"email": "first@acme.test", "name": "First"}
    )
    listed = platform.as_tenant("acme", "GET", "/v1/users")

    assert created.status_code == 201
    assert created.headers[CELL_HEADER] == cell_id
    assert listed.status_code == 200
    assert [user["name"] for user in listed.json()["items"]] == ["First"]


def test_a3_fill_first_fills_a_cell_to_target_before_using_the_next(platform: Platform) -> None:
    platform.register_cell("cell-1", capacity=4)
    platform.register_cell("cell-2", capacity=4)

    placements = [platform.onboard(f"tenant-{n}") for n in range(1, 5)]

    # 75% of 4 = 3 tenants on cell-1, then cell-2 opens.
    assert placements == ["cell-1", "cell-1", "cell-1", "cell-2"]


def test_a3b_past_target_uses_remaining_room_and_warns(
    platform: Platform, caplog: pytest.LogCaptureFixture
) -> None:
    platform.register_cell("cell-1", capacity=4)
    for n in range(1, 4):
        platform.onboard(f"tenant-{n}")

    with caplog.at_level(logging.WARNING):
        fourth = platform.onboard("tenant-4")

    assert fourth == "cell-1"
    assert any("capacity low" in record.getMessage() for record in caplog.records)


def test_a4_draining_disabled_and_dedicated_cells_are_not_used_for_pooled_tenants(
    platform: Platform,
) -> None:
    platform.register_cell("cell-1")
    platform.register_cell("cell-2", type="dedicated", capacity=1)  # one tenant (A70)
    platform.set_cell_status("cell-1", "draining")

    assert platform.create_tenant("acme").status_code == 503

    platform.set_cell_status("cell-1", "disabled")
    assert platform.create_tenant("acme").status_code == 503

    platform.set_cell_status("cell-1", "active")
    assert platform.onboard("acme") == "cell-1"


def test_a5_no_room_anywhere_returns_503(platform: Platform) -> None:
    platform.register_cell("cell-1", capacity=2)
    platform.onboard("tenant-1")
    platform.onboard("tenant-2")

    response = platform.create_tenant("tenant-3")

    assert response.status_code == 503
    assert response.json()["error"] == "no_capacity"


def test_a6_tenants_cannot_see_each_others_data(platform: Platform) -> None:
    platform.register_cell("cell-1")
    assert platform.onboard("acme") == platform.onboard("globex")  # same shared cell
    user = platform.as_tenant(
        "acme", "POST", "/v1/users", json={"email": "secret@acme.test", "name": "Secret"}
    ).json()

    listed = platform.as_tenant("globex", "GET", "/v1/users")
    fetched = platform.as_tenant("globex", "GET", f"/v1/users/{user['id']}")
    deleted = platform.as_tenant("globex", "DELETE", f"/v1/users/{user['id']}")

    assert listed.json()["items"] == []
    assert fetched.status_code == 404
    assert deleted.status_code == 404
    assert platform.as_tenant("acme", "GET", f"/v1/users/{user['id']}").status_code == 200


def test_a7_cell_rejects_a_tenant_it_does_not_host(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.register_cell("cell-2")
    platform.onboard("acme")  # lands on cell-1

    response = platform.cell_clients["cell-2"].get("/v1/users", headers={TENANT_HEADER: "acme"})

    assert response.status_code == 421
    assert response.json()["error"] == "tenant_not_in_cell"
    assert response.headers[CELL_HEADER] == "cell-2"


def test_a8_router_rejects_missing_invalid_and_unknown_tenants(platform: Platform) -> None:
    platform.register_cell("cell-1")

    missing = platform.router.get("/v1/users")
    invalid = platform.as_tenant("../v1/cells", "GET", "/v1/users")
    unknown = platform.as_tenant("nobody", "GET", "/v1/users")

    assert (missing.status_code, missing.json()["error"]) == (400, "missing_tenant")
    assert (invalid.status_code, invalid.json()["error"]) == (400, "invalid_tenant")
    assert (unknown.status_code, unknown.json()["error"]) == (404, "unknown_tenant")


@pytest.mark.parametrize(
    "path",
    ["/internal/tenants", "/internal", "/v1/../internal/tenants", "/v1/%2e%2e/internal/tenants"],
)
def test_a9_router_never_exposes_internal_endpoints(platform: Platform, path: str) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.cells["cell-1"].requests.clear()

    response = platform.as_tenant(
        "acme", "GET", path, headers={INTERNAL_TOKEN_HEADER: CELL_TOKENS["cell-1"]}
    )

    assert response.status_code == 404
    assert not any("internal" in r.path for r in platform.cells["cell-1"].requests)


def test_a9_router_strips_internal_headers(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.cells["cell-1"].requests.clear()

    platform.as_tenant(
        "acme", "GET", "/v1/users", headers={INTERNAL_TOKEN_HEADER: CELL_TOKENS["cell-1"]}
    )

    forwarded = platform.cells["cell-1"].requests[-1]
    assert forwarded.path == "/v1/users"
    assert INTERNAL_TOKEN_HEADER.lower() not in forwarded.headers
    assert forwarded.headers[TENANT_HEADER.lower()] == "acme"


def test_a10_cached_tenants_keep_working_while_control_plane_is_down(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.onboard("globex")
    assert platform.as_tenant("acme", "GET", "/v1/users").status_code == 200  # cached

    platform.control_plane.reachable = False
    platform.clock.advance(ROUTE_CACHE_TTL * 10)  # cache entry is now stale

    still_working = platform.as_tenant("acme", "GET", "/v1/users")
    never_cached = platform.as_tenant("globex", "GET", "/v1/users")

    assert still_working.status_code == 200
    assert (never_cached.status_code, never_cached.json()["error"]) == (
        503,
        "routing_unavailable",
    )


def test_a11_router_refreshes_a_stale_route_and_retries_once(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.register_cell("cell-2")
    platform.onboard("acme")  # cell-1
    assert platform.as_tenant("acme", "GET", "/v1/users").headers[CELL_HEADER] == "cell-1"

    # Simulate a completed tenant move (stage 3) behind the router's back.
    cell_2 = platform.cell_clients["cell-2"]
    cell_1 = platform.cell_clients["cell-1"]
    cell_2_internal = {INTERNAL_TOKEN_HEADER: CELL_TOKENS["cell-2"]}
    cell_2.post("/internal/tenants", json={"tenant_id": "acme"}, headers=cell_2_internal)
    platform.stacks.start("cell-2", "acme", platform.cell_secrets["cell-2"].values["acme"])
    cell_2.post("/internal/tenants/acme/activate", headers=cell_2_internal)
    cell_1.delete("/internal/tenants/acme", headers={INTERNAL_TOKEN_HEADER: CELL_TOKENS["cell-1"]})
    platform.control_plane_app.state.store.assign_tenant("acme", "cell-2")

    response = platform.as_tenant("acme", "GET", "/v1/users")  # cache still says cell-1

    assert response.status_code == 200
    assert response.headers[CELL_HEADER] == "cell-2"


def test_a12_cell_outage_only_affects_that_cells_tenants(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")  # cell-1
    platform.register_cell("cell-2")
    platform.set_cell_status("cell-1", "draining")
    platform.onboard("globex")  # cell-2

    platform.cells["cell-1"].reachable = False

    down = platform.as_tenant("acme", "GET", "/v1/users")
    up = platform.as_tenant("globex", "GET", "/v1/users")

    assert (down.status_code, down.json()["error"]) == (502, "cell_unreachable")
    assert up.status_code == 200
    assert up.headers[CELL_HEADER] == "cell-2"


@pytest.mark.parametrize(
    ("token", "expected"),
    [(None, 401), (CELL_TOKENS["cell-2"], 401), (ADMIN_TOKEN, 401), (CELL_TOKENS["cell-1"], 200)],
)
def test_a13_cell_internal_api_accepts_only_its_own_credential(
    platform: Platform, token: str | None, expected: int
) -> None:
    headers = {} if token is None else {INTERNAL_TOKEN_HEADER: token}

    response = platform.cell_clients["cell-1"].get("/internal/tenants", headers=headers)

    assert response.status_code == expected


def test_a13_cells_cannot_be_registered_without_a_credential(platform: Platform) -> None:
    response = platform.admin.post(
        "/v1/cells", json={"cell_id": "cell-9", "url": "http://cell-9", "capacity": 4}
    )

    assert (response.status_code, response.json()["error"]) == (422, "missing_cell_credential")


@pytest.mark.parametrize(
    ("method", "path", "token", "expected"),
    [
        ("GET", "/v1/tenants", None, 401),
        ("GET", "/v1/tenants", "wrong-token", 401),
        ("GET", "/v1/tenants", ROUTER_TOKEN, 403),
        ("POST", "/v1/cells", ROUTER_TOKEN, 403),
        ("GET", "/v1/routing/tenants/acme", None, 401),
        ("GET", "/v1/routing/tenants/acme", ROUTER_TOKEN, 200),
        ("GET", "/v1/routing/tenants/acme", ADMIN_TOKEN, 200),
    ],
)
def test_a14_control_plane_apis_require_the_right_credential(
    platform: Platform, method: str, path: str, token: str | None, expected: int
) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    headers = {INTERNAL_TOKEN_HEADER: token or ""}
    if token is None:
        platform.admin.headers.pop(INTERNAL_TOKEN_HEADER)
        headers = {}

    response = platform.admin.request(method, path, headers=headers, json={})

    assert response.status_code == expected


def test_a15_failed_provisioning_does_not_create_the_tenant(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.cells["cell-1"].reachable = False

    failed = platform.create_tenant("acme")

    assert (failed.status_code, failed.json()["error"]) == (502, "provisioning_failed")
    assert platform.admin.get("/v1/tenants/acme").status_code == 404

    platform.cells["cell-1"].reachable = True
    assert platform.onboard("acme") == "cell-1"
