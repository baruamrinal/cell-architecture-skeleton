"""Stage 3b acceptance tests: promote, demote, drain and consolidate (A66-A70)."""

from __future__ import annotations

import pytest

from cellarch.common.headers import CELL_HEADER, TENANT_HEADER

from .conftest import UNCONFIGURED_CELL_TOKENS, Platform, PlatformClusters
from .test_tenant_moves import onboard_on, sign_up, two_cells, user_ids


def tenant(platform: Platform, tenant_id: str) -> dict[str, str]:
    result: dict[str, str] = platform.admin.get(f"/v1/tenants/{tenant_id}").json()
    return result


def served_by(platform: Platform, tenant_id: str) -> str:
    platform.clock.advance(60)  # past the router's route cache
    response = platform.router.get("/v1/users", headers={TENANT_HEADER: tenant_id})
    assert response.status_code == 200, response.text
    cell_id: str = response.headers[CELL_HEADER]
    return cell_id


def with_user(platform: Platform, tenant_id: str) -> list[str]:
    assert sign_up(platform, tenant_id, f"ana@{tenant_id}.test").status_code == 201
    return user_ids(platform, tenant_id)


def test_a66_promoting_gives_a_tenant_its_own_cell(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    users = with_user(platform, "acme")

    assert platform.cellctl("tenants", "promote", "acme", "--cell", "cell-4") == 0

    cell = platform.admin.get("/v1/cells/cell-4").json()
    assert (cell["type"], cell["capacity"], cell["tenant_count"]) == ("dedicated", 1, 1)
    assert (tenant(platform, "acme")["cell_id"], tenant(platform, "acme")["placement_mode"]) == (
        "cell-4",
        "dedicated",
    )
    assert served_by(platform, "acme") == "cell-4"
    assert user_ids(platform, "acme") == users


def test_a66_a_failed_promotion_removes_the_new_cell(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    users = with_user(platform, "acme")
    failing = PlatformClusters(platform, fail_install_on="cell-4")

    assert platform.cellctl("tenants", "promote", "acme", "--cell", "cell-4", clusters=failing) == 1

    assert (tenant(platform, "acme")["cell_id"], tenant(platform, "acme")["status"]) == (
        "cell-1",
        "active",
    )
    assert tenant(platform, "acme")["placement_mode"] == "pooled"
    assert platform.admin.get("/v1/cells/cell-4").status_code == 404
    assert not platform.cells["cell-4"].reachable  # its cluster is gone
    assert user_ids(platform, "acme") == users


def test_a67_demoting_returns_a_tenant_to_a_shared_cell(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    users = with_user(platform, "acme")
    assert platform.cellctl("tenants", "promote", "acme", "--cell", "cell-4") == 0

    assert platform.cellctl("tenants", "demote", "acme") == 0

    moved = tenant(platform, "acme")
    assert moved["cell_id"] in ("cell-1", "cell-2")
    assert moved["placement_mode"] == "pooled"
    assert served_by(platform, "acme") == moved["cell_id"]
    assert user_ids(platform, "acme") == users
    assert platform.admin.get("/v1/cells/cell-4").status_code == 404
    assert not platform.cells["cell-4"].reachable


def test_a67_demoting_without_room_changes_nothing(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    assert platform.cellctl("tenants", "promote", "acme", "--cell", "cell-4") == 0
    for cell_id in ("cell-1", "cell-2"):
        platform.set_cell_status(cell_id, "draining")

    assert platform.cellctl("tenants", "demote", "acme") == 1

    assert (tenant(platform, "acme")["cell_id"], tenant(platform, "acme")["status"]) == (
        "cell-4",
        "active",
    )
    assert platform.admin.get("/v1/cells/cell-4").json()["tenant_count"] == 1


def test_a68_draining_moves_every_tenant_off_the_cell(platform: Platform) -> None:
    two_cells(platform)
    for tenant_id in ("acme", "globex", "initech"):
        assert platform.onboard(tenant_id) == "cell-1"
    users = with_user(platform, "globex")

    assert platform.cellctl("cells", "drain", "cell-1") == 0

    cell = platform.admin.get("/v1/cells/cell-1").json()
    assert (cell["status"], cell["tenant_count"]) == ("draining", 0)
    for tenant_id in ("acme", "globex", "initech"):
        assert tenant(platform, tenant_id)["cell_id"] == "cell-2"
    assert user_ids(platform, "globex") == users
    assert platform.onboard("hooli") == "cell-2"  # nothing new lands on a draining cell


def test_a68_draining_is_refused_without_room_elsewhere(platform: Platform) -> None:
    two_cells(platform)
    for tenant_id in ("acme", "globex", "initech"):
        platform.onboard(tenant_id)
    platform.admin.patch("/v1/cells/cell-2", json={"capacity": 2})

    assert platform.cellctl("cells", "drain", "cell-1") == 1

    cell = platform.admin.get("/v1/cells/cell-1").json()
    assert (cell["status"], cell["tenant_count"]) == ("active", 3)
    assert platform.admin.get("/v1/moves").json()["items"] == []


def test_a68_a_drain_that_stopped_part_way_can_be_run_again(platform: Platform) -> None:
    two_cells(platform)
    for tenant_id in ("acme", "globex", "initech"):
        platform.onboard(tenant_id)
    users = with_user(platform, "globex")

    stuck = PlatformClusters(platform, fail_tenant="globex")
    assert platform.cellctl("cells", "drain", "cell-1", clusters=stuck) == 1
    assert tenant(platform, "acme")["cell_id"] == "cell-2"
    assert (tenant(platform, "globex")["cell_id"], tenant(platform, "globex")["status"]) == (
        "cell-1",
        "active",
    )

    assert platform.cellctl("cells", "drain", "cell-1") == 0
    assert platform.admin.get("/v1/cells/cell-1").json()["tenant_count"] == 0
    assert user_ids(platform, "globex") == users


def test_a69_consolidating_empties_and_removes_a_cell(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    onboard_on(platform, "globex", "cell-2")
    users = with_user(platform, "globex")

    assert platform.cellctl("cells", "consolidate", "cell-2", "--delete-data") == 0

    assert tenant(platform, "globex")["cell_id"] == "cell-1"
    assert served_by(platform, "globex") == "cell-1"
    assert user_ids(platform, "globex") == users
    assert platform.admin.get("/v1/cells/cell-2").status_code == 404
    assert not platform.cells["cell-2"].reachable


def test_a70_a_dedicated_cell_holds_exactly_one_tenant(platform: Platform) -> None:
    two_cells(platform)
    credential = UNCONFIGURED_CELL_TOKENS["cell-3"]
    body = {
        "cell_id": "cell-3",
        "url": "http://cell-3",
        "type": "dedicated",
        "credential": credential,
    }
    too_big = platform.admin.post("/v1/cells", json={**body, "capacity": 4})
    assert (too_big.status_code, too_big.json()["error"]) == (422, "dedicated_capacity")

    dedicated = platform.admin.post("/v1/cells", json=body)
    assert (dedicated.status_code, dedicated.json()["capacity"]) == (201, 1)
    resized = platform.admin.patch("/v1/cells/cell-3", json={"capacity": 2})
    assert (resized.status_code, resized.json()["error"]) == (422, "dedicated_capacity")

    platform.onboard("acme")
    platform.onboard("globex")
    assert platform.move("acme", "cell-3")["state"] == "completed"
    second = platform.admin.post("/v1/tenants/globex/moves", json={"target_cell_id": "cell-3"})
    assert (second.status_code, second.json()["error"]) == (409, "cell_full")
    assert platform.onboard("initech") in ("cell-1", "cell-2")


@pytest.mark.parametrize(
    "command", [("tenants", "demote", "acme"), ("cells", "consolidate", "cell-4")]
)
def test_a70_dedicated_and_shared_cells_are_not_mixed_up(
    platform: Platform, command: tuple[str, ...]
) -> None:
    two_cells(platform)
    platform.onboard("acme")
    if command[1] == "consolidate":
        assert platform.cellctl("tenants", "promote", "acme", "--cell", "cell-4") == 0
    # Demoting a pooled tenant, or consolidating a dedicated cell, is refused.
    assert platform.cellctl(*command) == 1
