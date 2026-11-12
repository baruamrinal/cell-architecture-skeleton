"""Stage 2.5 acceptance tests: each tenant's own stack and database in its cell (A50-A56)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from cellarch.cli.api import CommandError
from cellarch.cli.clusters import ClusterError
from cellarch.cli.onboarding import onboard
from cellarch.common.headers import CELL_HEADER, TENANT_HEADER
from cellarch.data_plane.cell.tenant_resources import PostgresTenantDatabases

from .conftest import CHARTS, InProcessStacks, Platform

LINES = [{"sku": "book-1", "quantity": 2, "unit_price_cents": 1250}]


def sign_up(platform: Platform, tenant_id: str, email: str = "ana@example.test") -> Any:
    response = platform.as_tenant(
        tenant_id, "POST", "/v1/users", json={"email": email, "name": "Ana"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def tenant_database(platform: Platform, cell_id: str, tenant_id: str) -> Path:
    return platform.tmp_path / cell_id / "tenants" / f"{tenant_id}.db"


def test_a50_onboarding_gives_a_tenant_its_own_running_stack(platform: Platform) -> None:
    platform.register_cell("cell-1")

    cell_id = platform.onboard("acme")

    tenant = platform.admin.get("/v1/tenants/acme").json()
    assert (cell_id, tenant["status"]) == ("cell-1", "active")
    assert "acme" in platform.cell_secrets["cell-1"].values  # logins + stack credential
    assert platform.stacks.running("cell-1", "acme")
    user = sign_up(platform, "acme")
    order = platform.as_tenant(
        "acme", "POST", "/v1/orders", json={"user_id": user["id"], "lines": LINES}
    )
    assert (order.status_code, order.json()["total_cents"]) == (201, 2500)
    assert order.headers[CELL_HEADER] == "cell-1"
    assert tenant_database(platform, "cell-1", "acme").is_file()


def test_a51_a_tenant_without_a_running_stack_stays_provisioning(platform: Platform) -> None:
    platform.register_cell("cell-1")
    assert platform.create_tenant("acme").json()["status"] == "provisioning"

    activation = platform.admin.post("/v1/tenants/acme/activate")
    via_router = platform.as_tenant("acme", "GET", "/v1/users")
    direct = platform.cell_clients["cell-1"].get("/v1/users", headers={TENANT_HEADER: "acme"})

    assert (activation.status_code, activation.json()["error"]) == (409, "tenant_stack_not_ready")
    assert platform.admin.get("/v1/tenants/acme").json()["status"] == "provisioning"
    assert (via_router.status_code, via_router.json()["error"]) == (503, "tenant_unavailable")
    assert (direct.status_code, direct.json()["error"]) == (503, "tenant_not_ready")


def test_a52_tenants_on_one_cell_each_have_their_own_data(platform: Platform) -> None:
    platform.register_cell("cell-1")
    assert platform.onboard("acme") == platform.onboard("globex")  # same shared cell
    acme_user = sign_up(platform, "acme", "same@example.test")
    globex_user = sign_up(platform, "globex", "same@example.test")  # no clash: own database

    acme_list = platform.as_tenant("acme", "GET", "/v1/users").json()["items"]
    globex_list = platform.as_tenant("globex", "GET", "/v1/users").json()["items"]
    read_other = platform.as_tenant("globex", "GET", f"/v1/users/{acme_user['id']}")
    order_other = platform.as_tenant(
        "globex", "POST", "/v1/orders", json={"user_id": acme_user["id"], "lines": LINES}
    )

    assert [u["id"] for u in acme_list] == [acme_user["id"]]
    assert [u["id"] for u in globex_list] == [globex_user["id"]]
    assert read_other.status_code == 404
    assert (order_other.status_code, order_other.json()["error"]) == (422, "unknown_user")
    assert platform.as_tenant("globex", "GET", "/v1/orders").json()["items"] == []


def test_a53_one_tenants_stack_failing_affects_only_that_tenant(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.onboard("globex")
    platform.stacks.down.add("acme")

    down = platform.as_tenant("acme", "GET", "/v1/users")
    fine = platform.as_tenant("globex", "GET", "/v1/users")

    assert (down.status_code, down.json()["error"]) == (503, "tenant_stack_unavailable")
    assert down.headers["Retry-After"]
    assert fine.status_code == 200


def test_a54_deleting_a_tenant_removes_its_stack_and_data(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    sign_up(platform, "acme")

    platform.offboard("acme")

    assert not platform.stacks.running("cell-1", "acme")
    assert "acme" not in platform.cell_secrets["cell-1"].values
    assert not tenant_database(platform, "cell-1", "acme").exists()
    assert platform.admin.get("/v1/tenants/acme").status_code == 404
    gone = platform.as_tenant("acme", "GET", "/v1/users")
    assert (gone.status_code, gone.json()["error"]) == (404, "unknown_tenant")
    assert platform.admin.get("/v1/cells/cell-1").json()["tenant_count"] == 0


class FailingStacks(InProcessStacks):
    def helm_upgrade(self, *args: Any, **kwargs: Any) -> None:
        raise ClusterError("the stack's pods never became ready")


@pytest.mark.parametrize("failure", ["no-capacity", "cell-refuses", "stack-fails"])
def test_a55_failed_onboarding_leaves_nothing_behind(platform: Platform, failure: str) -> None:
    platform.register_cell("cell-1")
    stacks = InProcessStacks(platform)
    if failure == "no-capacity":
        platform.set_cell_status("cell-1", "draining")
    elif failure == "cell-refuses":
        platform.cell_secrets["cell-1"].fail = True
    else:
        stacks = FailingStacks(platform)

    body = {"tenant_id": "acme", "name": "Acme", "tier": "standard"}
    with pytest.raises((CommandError, ClusterError)):
        onboard(platform.api(), stacks, CHARTS, body, progress=print, sleep=lambda s: None)

    assert platform.admin.get("/v1/tenants").json() == []
    assert not platform.stacks.running("cell-1", "acme")
    assert "acme" not in platform.cell_secrets["cell-1"].values
    assert not tenant_database(platform, "cell-1", "acme").exists()


def _as_login(url: str, database: str | None = None) -> str:
    if database is None:
        return url
    return urlunsplit(urlsplit(url)._replace(path=f"/{database}"))


def test_a56_each_service_login_reaches_only_its_own_schema(postgres_url: str) -> None:
    databases = PostgresTenantDatabases(postgres_url)
    acme, globex = databases.create("acme"), databases.create("globex")
    try:
        with psycopg.connect(acme["user-account"], autocommit=True) as conn:
            conn.execute("CREATE TABLE users (id TEXT)")  # its own schema, by default
            schema = conn.execute("SELECT current_schema()").fetchone()
            assert schema == ("user_account",)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE order_processing.orders (id TEXT)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE public.leak (id TEXT)")
        with psycopg.connect(acme["order-processing"], autocommit=True) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM user_account.users")
            timeout = conn.execute("SHOW statement_timeout").fetchone()
            assert timeout == ("5s",)
        other_database = urlsplit(globex["user-account"]).path[1:]
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(_as_login(acme["user-account"], other_database))
    finally:
        databases.drop("acme")
        databases.drop("globex")
