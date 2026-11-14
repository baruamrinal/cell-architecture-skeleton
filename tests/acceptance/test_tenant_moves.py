"""Stage 3 acceptance tests: moving a tenant between cells (A57-A64) and the audit log (A65)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.cli.api import CommandError
from cellarch.cli.cellctl import main
from cellarch.cli.clusters import ClusterError
from cellarch.common.headers import CELL_HEADER, INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.common.sql import open_database, sqlite_url
from cellarch.data_plane.cell.tenant_data import TenantDataError, export_tenant, import_tenant
from cellarch.data_plane.cell.tenant_resources import PostgresTenantDatabases
from cellarch.services.common.settings import ServiceSettings
from cellarch.services.order_processing.app import OrderSettings
from cellarch.services.order_processing.app import create_app as create_order_processing
from cellarch.services.user_account.app import create_app as create_user_account
from tests.fakes import async_client

from .conftest import ADMIN_TOKEN, CELL_TOKENS, ROUTE_CACHE_TTL, InProcessStacks, Platform

LINES = [{"sku": "book-1", "quantity": 2, "unit_price_cents": 1250}]


def sign_up(platform: Platform, tenant_id: str, email: str, **kwargs: Any) -> httpx.Response:
    return platform.as_tenant(
        tenant_id, "POST", "/v1/users", json={"email": email, "name": "Ana"}, **kwargs
    )


def user_ids(platform: Platform, tenant_id: str, via: TestClient | None = None) -> list[str]:
    client = via or platform.router
    response = client.get("/v1/users", headers={TENANT_HEADER: tenant_id})
    assert response.status_code == 200, response.text
    return [u["id"] for u in response.json()["items"]]


def tenant_database(platform: Platform, cell_id: str, tenant_id: str) -> Path:
    return platform.tmp_path / cell_id / "tenants" / f"{tenant_id}.db"


def hosted(platform: Platform, cell_id: str) -> list[str]:
    response = platform.cell_clients[cell_id].get(
        "/internal/tenants", headers={INTERNAL_TOKEN_HEADER: CELL_TOKENS[cell_id]}
    )
    return [t["tenant_id"] for t in response.json()]


def moves(platform: Platform, tenant_id: str) -> list[dict[str, Any]]:
    response = platform.admin.get("/v1/moves", params={"tenant_id": tenant_id})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def two_cells(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.register_cell("cell-2")


def onboard_on(platform: Platform, tenant_id: str, cell_id: str) -> None:
    """Onboard a tenant onto a given cell by draining the others for a moment."""
    others = [
        c["cell_id"] for c in platform.admin.get("/v1/cells").json() if c["cell_id"] != cell_id
    ]
    for other in others:
        platform.set_cell_status(other, "draining")
    assert platform.onboard(tenant_id) == cell_id
    for other in others:
        platform.set_cell_status(other, "active")


def test_a57_a_moved_tenant_keeps_its_data_on_the_new_cell(platform: Platform) -> None:
    two_cells(platform)
    assert platform.onboard("acme") == "cell-1"
    first = sign_up(platform, "acme", "ana@example.test", headers={"Idempotency-Key": "k-1"})
    assert first.status_code == 201, first.text
    for email in ("bo@example.test", "cy@example.test"):
        assert sign_up(platform, "acme", email).status_code == 201
    order = platform.as_tenant(
        "acme", "POST", "/v1/orders", json={"user_id": first.json()["id"], "lines": LINES}
    )
    assert order.status_code == 201
    before = user_ids(platform, "acme")
    page = platform.as_tenant("acme", "GET", "/v1/users", params={"limit": 2}).json()

    finished = platform.move("acme", "cell-2")

    assert (finished["state"], finished["cleanup_pending"]) == ("completed", None)
    assert (finished["source_cell_id"], finished["target_cell_id"]) == ("cell-1", "cell-2")
    tenant = platform.admin.get("/v1/tenants/acme").json()
    assert (tenant["cell_id"], tenant["status"]) == ("cell-2", "active")
    served = platform.as_tenant("acme", "GET", "/v1/users")
    assert served.headers[CELL_HEADER] == "cell-2"
    assert user_ids(platform, "acme") == before
    orders = platform.as_tenant("acme", "GET", "/v1/orders").json()["items"]
    assert [o["id"] for o in orders] == [order.json()["id"]]
    rest = platform.as_tenant("acme", "GET", "/v1/users", params={"cursor": page["next_cursor"]})
    assert [u["id"] for u in rest.json()["items"]] == before[2:]  # old cursors still work
    replay = sign_up(platform, "acme", "ana@example.test", headers={"Idempotency-Key": "k-1"})
    assert (replay.status_code, replay.json()["id"]) == (201, first.json()["id"])
    assert replay.headers["Idempotent-Replay"] == "true"
    new = sign_up(platform, "acme", "dee@example.test")
    assert new.status_code == 201, new.text
    assert user_ids(platform, "acme") == [*before, new.json()["id"]]  # keys continue
    # Nothing of it is left on the old cell.
    assert not platform.stacks.running("cell-1", "acme")
    assert "acme" not in platform.cell_secrets["cell-1"].values
    assert not tenant_database(platform, "cell-1", "acme").exists()
    assert hosted(platform, "cell-1") == []
    assert platform.admin.get("/v1/cells/cell-1").json()["tenant_count"] == 0


def test_a58_writes_pause_while_a_tenant_is_moving(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    assert sign_up(platform, "acme", "ana@example.test").status_code == 201
    stale = platform.extra_router()
    assert user_ids(platform, "acme", via=stale)  # caches the route while it is `active`
    seen: dict[str, Any] = {}

    def half_way(cell_id: str, tenant_id: str) -> None:
        seen["status"] = platform.admin.get("/v1/tenants/acme").json()["status"]
        # A router whose cached route is still `active`: the frozen source pauses writes.
        seen["stale_read"] = stale.get("/v1/users", headers={TENANT_HEADER: "acme"})
        seen["stale_write"] = stale.post(
            "/v1/users",
            json={"email": "x@example.test", "name": "X"},
            headers={TENANT_HEADER: "acme"},
        )
        # A router with a fresh route sees `migrating` and pauses writes itself.
        platform.clock.advance(ROUTE_CACHE_TTL + 1)
        seen["read"] = platform.as_tenant("acme", "GET", "/v1/users")
        seen["write"] = sign_up(platform, "acme", "y@example.test")

    platform.move("acme", "cell-2", InProcessStacks(platform, after_install=half_way))

    assert seen["status"] == "migrating"
    for read in (seen["read"], seen["stale_read"]):
        assert (read.status_code, read.headers[CELL_HEADER]) == (200, "cell-1")
    for write in (seen["write"], seen["stale_write"]):
        assert (write.status_code, write.json()["error"]) == (503, "tenant_migrating")
        assert write.headers["Retry-After"]
    assert CELL_HEADER not in seen["write"].headers  # paused by the router itself
    assert seen["stale_write"].headers[CELL_HEADER] == "cell-1"  # by the frozen source


class _Breakage:
    """Ways a move can fail before the switch."""

    @staticmethod
    def stacks(platform: Platform, failure: str) -> InProcessStacks:
        if failure == "stack-never-starts":
            return InProcessStacks(platform, fail_install_on="cell-2")
        if failure == "copy-refused":

            def leftover_data(cell_id: str, tenant_id: str) -> None:
                # The target already holds data: the import refuses to mix it in.
                url = sqlite_url(tenant_database(platform, cell_id, tenant_id))
                with open_database(url).connect() as conn:
                    conn.execute(
                        "INSERT INTO user_account_users (id, tenant_id, email, name, created_at) "
                        "VALUES ('usr_x', 'acme', 'x@example.test', 'X', 'now')"
                    )

            return InProcessStacks(platform, after_install=leftover_data)

        def stack_goes_down(cell_id: str, tenant_id: str) -> None:
            platform.stacks.down.add(tenant_id)

        return InProcessStacks(platform, after_install=stack_goes_down)


@pytest.mark.parametrize("failure", ["stack-never-starts", "copy-refused", "stack-not-ready"])
def test_a59_a_failed_move_is_rolled_back(platform: Platform, failure: str) -> None:
    two_cells(platform)
    platform.onboard("acme")
    assert sign_up(platform, "acme", "ana@example.test").status_code == 201
    before = user_ids(platform, "acme")

    with pytest.raises((CommandError, ClusterError)):
        platform.move("acme", "cell-2", _Breakage.stacks(platform, failure))
    platform.stacks.down.discard("acme")

    tenant = platform.admin.get("/v1/tenants/acme").json()
    assert (tenant["cell_id"], tenant["status"]) == ("cell-1", "active")
    platform.clock.advance(ROUTE_CACHE_TTL + 1)
    assert user_ids(platform, "acme") == before
    assert sign_up(platform, "acme", "bo@example.test").status_code == 201  # unfrozen
    assert not platform.stacks.running("cell-2", "acme")
    assert "acme" not in platform.cell_secrets["cell-2"].values
    assert not tenant_database(platform, "cell-2", "acme").exists()
    assert hosted(platform, "cell-2") == []
    [move] = moves(platform, "acme")
    assert (move["state"], move["cleanup_pending"]) == ("rolled_back", None)
    assert move["error"]


def test_a60_a_leftover_on_the_old_cell_is_flagged_for_clean_up(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    assert sign_up(platform, "acme", "ana@example.test").status_code == 201
    platform.cell_secrets["cell-1"].fail_delete = True

    finished = platform.move("acme", "cell-2")

    assert (finished["state"], finished["cleanup_pending"]) == ("completed", "cell-1")
    platform.clock.advance(ROUTE_CACHE_TTL + 1)
    served = platform.as_tenant("acme", "GET", "/v1/users")
    assert (served.status_code, served.headers[CELL_HEADER]) == (200, "cell-2")
    # Cleaning up later finishes the job.
    platform.cell_secrets["cell-1"].fail_delete = False
    retried = platform.admin.post(f"/v1/moves/{finished['move_id']}/complete")
    assert (retried.status_code, retried.json()["cleanup_pending"]) == (200, None)
    assert hosted(platform, "cell-1") == []


def test_a61_a_router_with_the_old_route_finds_the_new_cell(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    assert sign_up(platform, "acme", "ana@example.test").status_code == 201
    stale = platform.extra_router()
    before = user_ids(platform, "acme", via=stale)  # cached: acme -> cell-1

    platform.move("acme", "cell-2")

    response = stale.get("/v1/users", headers={TENANT_HEADER: "acme"})
    assert (response.status_code, response.headers[CELL_HEADER]) == (200, "cell-2")
    assert [u["id"] for u in response.json()["items"]] == before


@pytest.mark.parametrize(
    ("case", "status", "error"),
    [
        ("same-cell", 422, "same_cell"),
        ("unknown-cell", 404, "cell_not_found"),
        ("inactive-cell", 409, "cell_not_active"),
        ("full-cell", 409, "cell_full"),
        ("tenant-not-active", 409, "tenant_not_active"),
        ("already-moving", 409, "move_in_progress"),
    ],
)
def test_a62_moves_that_cannot_work_are_refused(
    platform: Platform, case: str, status: int, error: str
) -> None:
    two_cells(platform)
    platform.onboard("acme")
    tenant_id, target = "acme", "cell-2"
    if case == "same-cell":
        target = "cell-1"
    elif case == "unknown-cell":
        target = "cell-9"
    elif case == "inactive-cell":
        platform.set_cell_status("cell-2", "draining")
    elif case == "full-cell":
        platform.admin.patch("/v1/cells/cell-2", json={"capacity": 1})
        onboard_on(platform, "globex", "cell-2")
    elif case == "tenant-not-active":
        tenant_id = "initech"
        assert platform.create_tenant("initech").status_code == 201  # still provisioning
    else:
        started = platform.admin.post("/v1/tenants/acme/moves", json={"target_cell_id": "cell-2"})
        assert started.status_code == 201, started.text
    tenant_before = platform.admin.get(f"/v1/tenants/{tenant_id}").json()
    moves_before = moves(platform, tenant_id)

    response = platform.admin.post(
        f"/v1/tenants/{tenant_id}/moves", json={"target_cell_id": target}
    )

    assert (response.status_code, response.json()["error"]) == (status, error)
    assert platform.admin.get(f"/v1/tenants/{tenant_id}").json() == tenant_before
    assert moves(platform, tenant_id) == moves_before


def test_a63_a_moved_tenant_shares_no_data_with_its_new_neighbour(platform: Platform) -> None:
    two_cells(platform)
    platform.onboard("acme")
    onboard_on(platform, "globex", "cell-2")
    acme_user = sign_up(platform, "acme", "same@example.test").json()
    globex_user = sign_up(platform, "globex", "same@example.test").json()

    platform.move("acme", "cell-2")

    assert user_ids(platform, "acme") == [acme_user["id"]]
    assert user_ids(platform, "globex") == [globex_user["id"]]
    for tenant_id, other in (("acme", globex_user), ("globex", acme_user)):
        read = platform.as_tenant(tenant_id, "GET", f"/v1/users/{other['id']}")
        assert read.status_code == 404
        order = platform.as_tenant(
            tenant_id, "POST", "/v1/orders", json={"user_id": other["id"], "lines": LINES}
        )
        assert (order.status_code, order.json()["error"]) == (422, "unknown_user")


def _start_services(urls: dict[str, str]) -> tuple[TestClient, TestClient]:
    """The tenant's services on their databases (they apply their own migrations)."""
    users = create_user_account(ServiceSettings("acme", "stack", urls["user-account"]))
    orders = create_order_processing(
        OrderSettings("acme", "stack", urls["order-processing"], user_account_url="http://ua"),
        http_client=async_client(transport=httpx.ASGITransport(app=users), base_url="http://ua"),
    )
    return TestClient(users), TestClient(orders)


def test_a64_postgres_export_and_import_keep_every_row_and_key(postgres_url: str) -> None:
    databases = PostgresTenantDatabases(postgres_url)
    headers = {INTERNAL_TOKEN_HEADER: "stack", TENANT_HEADER: "acme"}
    try:
        users, orders = _start_services(databases.create("acme"))
        created = [
            users.post(
                "/v1/users",
                json={"email": f"u{i}@example.test", "name": f"U{i}"},
                headers={**headers, "Idempotency-Key": f"k-{i}"},
            ).json()
            for i in range(3)
        ]
        order = orders.post(
            "/v1/orders", json={"user_id": created[0]["id"], "lines": LINES}, headers=headers
        ).json()
        export = list(export_tenant(databases, "acme"))

        # Another server: a fresh, empty database whose services created their tables.
        databases.drop("acme")
        users, orders = _start_services(databases.create("acme"))
        counts = import_tenant(databases, "acme", export)

        assert counts["user_account_users"] == 3
        assert counts["order_processing_orders"] == 1
        assert counts["user_account_idempotency_keys"] == 3
        listed = users.get("/v1/users", headers=headers).json()["items"]
        assert [u["id"] for u in listed] == [u["id"] for u in created]
        assert orders.get(f"/v1/orders/{order['id']}", headers=headers).status_code == 200
        replay = users.post(
            "/v1/users",
            json={"email": "u0@example.test", "name": "U0"},
            headers={**headers, "Idempotency-Key": "k-0"},
        )
        assert replay.json()["id"] == created[0]["id"]
        fresh = users.post(
            "/v1/users", json={"email": "new@example.test", "name": "N"}, headers=headers
        )
        assert fresh.status_code == 201, fresh.text  # the sequence continues after the import

        with pytest.raises(TenantDataError) as not_empty:
            import_tenant(databases, "acme", export)
        assert not_empty.value.code == "tenant_data_not_empty"
        header = json.loads(export[0])
        header["services"]["user-account"]["schema_version"] = 99
        with pytest.raises(TenantDataError) as mismatch:
            import_tenant(databases, "acme", [json.dumps(header).encode(), *export[1:]])
        assert mismatch.value.code == "schema_version_mismatch"
    finally:
        databases.drop("acme")


def test_a65_every_admin_change_is_in_the_audit_log(
    platform: Platform, capsys: pytest.CaptureFixture[str]
) -> None:
    two_cells(platform)
    platform.onboard("acme")
    platform.set_limit("acme", 10, 20)
    refused = platform.admin.post("/v1/tenants/acme/moves", json={"target_cell_id": "cell-1"})
    assert refused.status_code == 422
    platform.move("acme", "cell-2")
    platform.offboard("acme")

    log = platform.admin.get("/v1/audit", params={"limit": 100}).json()["items"]
    actions = [(e["action"], e["outcome"]) for e in reversed(log)]  # newest first
    assert actions == [
        ("cell.register", "succeeded"),
        ("cell.register", "succeeded"),
        ("tenant.create", "succeeded"),
        ("tenant.activate", "succeeded"),
        ("tenant.rate_limit.set", "succeeded"),
        ("move.start", "refused"),
        ("move.start", "succeeded"),
        ("move.copy", "succeeded"),
        ("move.switch", "succeeded"),
        ("move.complete", "succeeded"),
        ("tenant.delete", "succeeded"),
    ]
    switch = next(e for e in log if e["action"] == "move.switch")
    assert switch["tenant_id"] == "acme"
    assert (switch["detail"]["source_cell_id"], switch["detail"]["target_cell_id"]) == (
        "cell-1",
        "cell-2",
    )
    assert switch["actor"] == "admin" and switch["at"]
    assert next(e for e in log if e["outcome"] == "refused")["error"] == "same_cell"

    # Cursor pagination, newest first, and filters.
    first = platform.admin.get("/v1/audit", params={"limit": 4}).json()
    second = platform.admin.get(
        "/v1/audit", params={"limit": 4, "cursor": first["next_cursor"]}
    ).json()
    assert [e["event_id"] for e in first["items"] + second["items"]] == [
        e["event_id"] for e in log[:8]
    ]
    by_cell = platform.admin.get("/v1/audit", params={"cell_id": "cell-2", "limit": 100}).json()
    assert by_cell["items"] and all(
        "cell-2" in (e["cell_id"], e["detail"].get("target_cell_id")) for e in by_cell["items"]
    )
    assert platform.admin.get("/v1/audit", params={"tenant_id": "globex"}).json()["items"] == []

    # cellctl prints it.
    code = main(["audit", "--tenant", "acme"], client=platform.admin, token=ADMIN_TOKEN)
    out = capsys.readouterr().out
    assert code == 0
    assert "move.switch" in out and "tenant.delete" in out
