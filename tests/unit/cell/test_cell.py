"""The cell gateway: tenant registry, onboarding steps, and routing to each tenant's stack."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cellarch.common.config import ConfigError
from cellarch.common.headers import CELL_HEADER, INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.data_plane.cell.app import CellSettings, create_app
from cellarch.data_plane.cell.store import CellStore
from cellarch.data_plane.cell.tenant_resources import (
    STACK_TOKEN_KEY,
    SqliteTenantDatabases,
    TenantResourceError,
    database_url_key,
)
from tests.fakes import MemorySecrets, StackHost, async_client, stack_url_template

TOKEN = "cell-token"
INTERNAL = {INTERNAL_TOKEN_HEADER: TOKEN}


class TestCellStore:
    @pytest.fixture
    def store(self, tmp_path: Path) -> CellStore:
        return CellStore(str(tmp_path / "cell.db"))

    def test_tenants_start_provisioning_and_are_activated(self, store: CellStore) -> None:
        first = store.add_tenant("acme", "stack-token")
        assert first["status"] == "provisioning"
        assert "stack_token" not in first
        assert store.stack_token("acme") == "stack-token"
        store.set_status("acme", "active")
        assert store.get_tenant("acme")["status"] == "active"  # type: ignore[index]
        assert [t["tenant_id"] for t in store.list_tenants()] == ["acme"]

    def test_unknown_tenants(self, store: CellStore) -> None:
        assert store.get_tenant("nobody") is None
        assert store.stack_token("nobody") is None
        assert store.delete_tenant("nobody") is False

    def test_delete(self, store: CellStore) -> None:
        store.add_tenant("acme", "t")
        assert store.delete_tenant("acme") is True
        assert store.get_tenant("acme") is None


@dataclass
class Cell:
    client: TestClient
    secrets: MemorySecrets
    stacks: StackHost
    databases: SqliteTenantDatabases

    def provision(self, tenant_id: str = "acme") -> dict[str, str]:
        response = self.client.post(
            "/internal/tenants", json={"tenant_id": tenant_id}, headers=INTERNAL
        )
        assert response.status_code == 201, response.text
        result: dict[str, str] = response.json()
        return result

    def start_stack(self, tenant_id: str = "acme") -> None:
        self.stacks.start("cell-x", tenant_id, self.secrets.values[tenant_id])

    def activate(self, tenant_id: str = "acme") -> int:
        response = self.client.post(f"/internal/tenants/{tenant_id}/activate", headers=INTERNAL)
        status: int = response.status_code
        return status

    def onboard(self, tenant_id: str = "acme") -> None:
        self.provision(tenant_id)
        self.start_stack(tenant_id)
        assert self.activate(tenant_id) == 200


@pytest.fixture
def cell(tmp_path: Path) -> Cell:
    return make_cell(tmp_path)


class TestRequests:
    def test_healthz_reports_cell(self, cell: Cell) -> None:
        response = cell.client.get("/healthz")
        assert response.json() == {"status": "ok", "cell_id": "cell-x"}
        assert response.headers[CELL_HEADER] == "cell-x"

    @pytest.mark.parametrize(
        ("headers", "status", "error"),
        [
            ({}, 400, "missing_tenant"),
            ({TENANT_HEADER: "Not Valid"}, 400, "invalid_tenant"),
            ({TENANT_HEADER: "nobody"}, 421, "tenant_not_in_cell"),
        ],
    )
    def test_tenant_checks(
        self, cell: Cell, headers: dict[str, str], status: int, error: str
    ) -> None:
        response = cell.client.get("/v1/users", headers=headers)
        assert (response.status_code, response.json()["error"]) == (status, error)
        assert response.headers[CELL_HEADER] == "cell-x"

    def test_routes_users_and_orders_to_the_tenants_stack(self, cell: Cell) -> None:
        cell.onboard()
        headers = {TENANT_HEADER: "acme", "X-Request-ID": "req-1"}
        user = cell.client.post(
            "/v1/users",
            json={"email": "ana@acme.test", "name": "Ana"},
            headers={**headers, "Idempotency-Key": "k1"},
        )
        replay = cell.client.post(
            "/v1/users",
            json={"email": "ana@acme.test", "name": "Ana"},
            headers={**headers, "Idempotency-Key": "k1"},
        )
        order = cell.client.post(
            "/v1/orders",
            json={
                "user_id": user.json()["id"],
                "lines": [{"sku": "a", "quantity": 1, "unit_price_cents": 5}],
            },
            headers=headers,
        )
        listed = cell.client.get("/v1/orders", params={"limit": 1}, headers=headers)
        assert user.status_code == 201
        assert replay.headers["Idempotent-Replay"] == "true"
        assert order.status_code == 201 and order.json()["total_cents"] == 5
        assert listed.json()["items"][0]["id"] == order.json()["id"]
        assert listed.headers[CELL_HEADER] == "cell-x"

    def test_repeated_query_parameters_reach_the_stack(self, cell: Cell) -> None:
        cell.onboard()
        headers = {TENANT_HEADER: "acme"}
        ids = [
            cell.client.post(
                "/v1/users", json={"email": f"u{i}@acme.test", "name": "U"}, headers=headers
            ).json()["id"]
            for i in range(2)
        ]
        for user_id in ids:
            cell.client.post(
                "/v1/orders",
                json={
                    "user_id": user_id,
                    "lines": [{"sku": "a", "quantity": 1, "unit_price_cents": 5}],
                },
                headers=headers,
            )
        listed = cell.client.get(
            "/v1/orders", params=[("user_id", ids[0]), ("user_id", ids[1])], headers=headers
        )
        assert sorted(o["user_id"] for o in listed.json()["items"]) == sorted(ids)

    def test_other_paths_are_not_routed(self, cell: Cell) -> None:
        cell.onboard()
        response = cell.client.get("/v1/items", headers={TENANT_HEADER: "acme"})
        assert response.status_code == 404

    def test_a_provisioning_tenant_is_not_ready(self, cell: Cell) -> None:
        cell.provision()
        response = cell.client.get("/v1/users", headers={TENANT_HEADER: "acme"})
        assert (response.status_code, response.json()["error"]) == (503, "tenant_not_ready")
        assert response.headers["Retry-After"]

    def test_an_unreachable_stack_is_unavailable(self, cell: Cell) -> None:
        cell.onboard()
        cell.stacks.down.add("acme")
        response = cell.client.get("/v1/users", headers={TENANT_HEADER: "acme"})
        assert (response.status_code, response.json()["error"]) == (503, "tenant_stack_unavailable")
        assert response.headers["Retry-After"]

    def test_client_cannot_override_the_stack_credential(self, cell: Cell) -> None:
        cell.onboard()
        response = cell.client.get(
            "/v1/users", headers={TENANT_HEADER: "acme", INTERNAL_TOKEN_HEADER: "forged"}
        )
        assert response.status_code == 200  # the gateway sets the stack credential itself


class TestInternalApi:
    def test_requires_the_cells_credential(self, cell: Cell) -> None:
        response = cell.client.get("/internal/tenants", headers={INTERNAL_TOKEN_HEADER: "wrong"})
        assert response.status_code == 401

    def test_provision_creates_database_and_secret(self, cell: Cell) -> None:
        created = cell.provision()
        assert created["status"] == "provisioning"
        secret = cell.secrets.values["acme"]
        assert secret[STACK_TOKEN_KEY]
        assert secret[database_url_key("user-account")].startswith("sqlite:///")
        assert STACK_TOKEN_KEY not in str(created)
        assert cell.provision() == created  # idempotent

    def test_provision_rejects_invalid_tenant_ids(self, cell: Cell) -> None:
        response = cell.client.post(
            "/internal/tenants", json={"tenant_id": "../x"}, headers=INTERNAL
        )
        assert response.status_code == 422

    def test_failed_provisioning_is_undone(self, cell: Cell) -> None:
        cell.secrets.fail = True
        response = cell.client.post(
            "/internal/tenants", json={"tenant_id": "acme"}, headers=INTERNAL
        )
        assert (response.status_code, response.json()["error"]) == (
            502,
            "tenant_provisioning_failed",
        )
        assert cell.client.get("/internal/tenants", headers=INTERNAL).json() == []

    def test_activation_needs_a_ready_stack(self, cell: Cell) -> None:
        cell.provision()
        response = cell.client.post("/internal/tenants/acme/activate", headers=INTERNAL)
        assert (response.status_code, response.json()["error"]) == (409, "tenant_stack_not_ready")
        cell.start_stack()
        assert cell.activate() == 200
        assert cell.activate() == 200  # idempotent
        tenants = cell.client.get("/internal/tenants", headers=INTERNAL).json()
        assert [(t["tenant_id"], t["status"]) for t in tenants] == [("acme", "active")]

    def test_a_hanging_readiness_probe_is_not_ready_rather_than_a_failure(
        self, tmp_path: Path
    ) -> None:
        """A new pod can swallow connections until its network policy is in place; the
        probe must give up well before the control plane's call to the cell does, so
        the answer is 409 (retry) rather than a timeout (onboarding undone)."""
        import httpx

        from cellarch.data_plane.cell.app import READINESS_PROBE_TIMEOUT_SECONDS

        timeouts: list[float | None] = []

        def stack(request: httpx.Request) -> httpx.Response:
            timeouts.append(request.extensions["timeout"]["read"])
            raise httpx.ReadTimeout("no answer", request=request)

        app = create_app(
            CellSettings(
                cell_id="cell-x",
                db_path=str(tmp_path / "c.db"),
                internal_token=TOKEN,
                tenant_db_admin_url=f"sqlite:///{tmp_path.as_posix()}/tenants",
            ),
            secrets=MemorySecrets(),
            http_client=async_client(transport=httpx.MockTransport(stack), timeout=10),
        )
        client = TestClient(app)
        client.post("/internal/tenants", json={"tenant_id": "acme"}, headers=INTERNAL)
        response = client.post("/internal/tenants/acme/activate", headers=INTERNAL)
        assert (response.status_code, response.json()["error"]) == (409, "tenant_stack_not_ready")
        assert timeouts and all(t == READINESS_PROBE_TIMEOUT_SECONDS for t in timeouts)
        assert READINESS_PROBE_TIMEOUT_SECONDS <= 3  # well under the control plane's 10 s

    def test_activating_an_unknown_tenant(self, cell: Cell) -> None:
        response = cell.client.post("/internal/tenants/nobody/activate", headers=INTERNAL)
        assert (response.status_code, response.json()["error"]) == (404, "tenant_not_found")

    def test_delete_drops_everything_and_is_idempotent(self, cell: Cell, tmp_path: Path) -> None:
        cell.onboard()
        cell.client.post(
            "/v1/users", json={"email": "a@acme.test", "name": "A"}, headers={TENANT_HEADER: "acme"}
        )
        assert cell.client.delete("/internal/tenants/acme", headers=INTERNAL).status_code == 204
        assert cell.client.delete("/internal/tenants/acme", headers=INTERNAL).status_code == 204
        assert "acme" not in cell.secrets.values
        assert not (tmp_path / "tenants" / "acme.db").exists()
        response = cell.client.get("/v1/users", headers={TENANT_HEADER: "acme"})
        assert response.status_code == 421

    def test_delete_reports_resource_failures(
        self, cell: Cell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cell.provision()

        def broken(tenant_id: str) -> None:
            raise TenantResourceError("database server down")

        monkeypatch.setattr(cell.secrets, "delete", broken)
        response = cell.client.delete("/internal/tenants/acme", headers=INTERNAL)
        assert (response.status_code, response.json()["error"]) == (502, "tenant_deletion_failed")
        assert [
            t["tenant_id"] for t in cell.client.get("/internal/tenants", headers=INTERNAL).json()
        ] == ["acme"]


class TestMoves:
    """The cell's part of a tenant move: freeze, export, import, unfreeze."""

    def freeze(self, cell: Cell, action: str = "freeze") -> int:
        response = cell.client.post(f"/internal/tenants/acme/{action}", headers=INTERNAL)
        status: int = response.status_code
        return status

    def sign_up(self, cell: Cell, email: str = "ana@example.test") -> int:
        response = cell.client.post(
            "/v1/users", json={"email": email, "name": "Ana"}, headers={TENANT_HEADER: "acme"}
        )
        status: int = response.status_code
        return status

    def test_a_frozen_tenant_serves_reads_and_pauses_writes(self, cell: Cell) -> None:
        cell.onboard()
        assert self.sign_up(cell) == 201
        assert self.freeze(cell) == 200
        assert self.freeze(cell) == 200  # idempotent

        read = cell.client.get("/v1/users", headers={TENANT_HEADER: "acme"})
        write = cell.client.post(
            "/v1/users",
            json={"email": "b@example.test", "name": "B"},
            headers={TENANT_HEADER: "acme"},
        )

        assert read.status_code == 200
        assert (write.status_code, write.json()["error"]) == (503, "tenant_migrating")
        assert write.headers["Retry-After"]
        assert self.freeze(cell, "unfreeze") == 200
        assert self.sign_up(cell, "c@example.test") == 201

    def test_only_active_tenants_can_be_frozen(self, cell: Cell) -> None:
        cell.provision()
        response = cell.client.post("/internal/tenants/acme/freeze", headers=INTERNAL)
        assert (response.status_code, response.json()["error"]) == (409, "tenant_not_active")
        missing = cell.client.post("/internal/tenants/nobody/freeze", headers=INTERNAL)
        assert missing.status_code == 404
        assert (
            cell.client.post("/internal/tenants/nobody/unfreeze", headers=INTERNAL).status_code
            == 404
        )

    def test_export_needs_a_frozen_tenant(self, cell: Cell) -> None:
        cell.onboard()
        response = cell.client.get("/internal/tenants/acme/export", headers=INTERNAL)
        assert (response.status_code, response.json()["error"]) == (409, "tenant_not_frozen")
        missing = cell.client.get("/internal/tenants/nobody/export", headers=INTERNAL)
        assert missing.status_code == 404

    def test_export_then_import_into_another_cell(self, cell: Cell, tmp_path: Path) -> None:
        cell.onboard()
        assert self.sign_up(cell) == 201
        self.freeze(cell)
        export = cell.client.get("/internal/tenants/acme/export", headers=INTERNAL)
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("application/x-ndjson")

        other = make_cell(tmp_path / "other")
        other.provision()
        other.start_stack()
        imported = other.client.post(
            "/internal/tenants/acme/import", content=export.content, headers=INTERNAL
        )

        assert imported.status_code == 200, imported.text
        assert imported.json()["tables"]["user_account_users"] == 1
        assert other.activate() == 200
        listed = other.client.get("/v1/users", headers={TENANT_HEADER: "acme"}).json()["items"]
        assert [u["email"] for u in listed] == ["ana@example.test"]

    def test_import_only_into_a_tenant_being_set_up(self, cell: Cell) -> None:
        cell.onboard()
        active = cell.client.post("/internal/tenants/acme/import", content=b"", headers=INTERNAL)
        assert (active.status_code, active.json()["error"]) == (409, "tenant_not_importable")
        missing = cell.client.post("/internal/tenants/nobody/import", content=b"", headers=INTERNAL)
        assert missing.status_code == 404

    def test_import_errors_keep_their_code(self, cell: Cell) -> None:
        cell.provision()  # its stack never started: no tables yet
        response = cell.client.post(
            "/internal/tenants/acme/import", content=b"{not json", headers=INTERNAL
        )
        assert (response.status_code, response.json()["error"]) == (422, "invalid_export")

    def test_database_failures_during_export_are_reported(
        self, cell: Cell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cell.onboard()
        self.freeze(cell)
        from cellarch.common.sql import DatabaseError
        from cellarch.data_plane.cell import app as gateway

        def broken(*args: object, **kwargs: object) -> object:
            raise DatabaseError("server gone")

        monkeypatch.setattr(gateway, "export_tenant", broken)
        response = cell.client.get("/internal/tenants/acme/export", headers=INTERNAL)
        assert (response.status_code, response.json()["error"]) == (502, "tenant_export_failed")
        monkeypatch.setattr(gateway, "import_tenant", broken)
        cell.provision("globex")
        response = cell.client.post(
            "/internal/tenants/globex/import", content=b"x", headers=INTERNAL
        )
        assert (response.status_code, response.json()["error"]) == (502, "tenant_import_failed")


def make_cell(tmp_path: Path, *, mgr: bool = True) -> Cell:
    secrets, stacks = MemorySecrets(), StackHost()
    app = create_app(
        CellSettings(
            cell_id="cell-x",
            db_path=str(tmp_path / "c.db"),
            internal_token=TOKEN,
            tenant_db_admin_url=f"sqlite:///{tmp_path.as_posix()}/tenants",
            tenant_service_url=stack_url_template("cell-x"),
            mgr_url="http://mgr.cell-x.stacks" if mgr else None,
            mgr_token="mgr-token" if mgr else None,
        ),
        secrets=secrets,
        http_client=stacks.client(),
    )
    return Cell(TestClient(app), secrets, stacks, SqliteTenantDatabases(str(tmp_path / "tenants")))


class TestSettings:
    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CELL_ID", "cell-7")
        monkeypatch.setenv("CELL_DB_PATH", "/data/cell-7.db")
        monkeypatch.setenv("CELL_INTERNAL_TOKEN", "t")
        monkeypatch.setenv("TENANT_DB_ADMIN_URL", "postgresql://postgres:pw@db/postgres")
        settings = CellSettings.from_env()
        assert settings.cell_id == "cell-7"
        assert settings.tenant_db_admin_url == "postgresql://postgres:pw@db/postgres"
        assert settings.tenant_service_url == "http://tenant-{tenant}-{service}:8000"
        assert "pw" not in repr(settings)

    def test_tenant_database_server_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CELL_ID", "cell-7")
        monkeypatch.setenv("CELL_INTERNAL_TOKEN", "t")
        monkeypatch.delenv("TENANT_DB_ADMIN_URL", raising=False)
        with pytest.raises(ConfigError, match="TENANT_DB_ADMIN_URL"):
            CellSettings.from_env()

    @pytest.mark.parametrize(
        "template", ["http://x:8000", "http://{tenant}:8000", "http://{tenant}-{service}-{other}"]
    )
    def test_service_url_template_is_validated(self, template: str, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="TENANT_SERVICE_URL"):
            CellSettings("c", str(tmp_path / "c.db"), "t", tenant_service_url=template)

    def test_without_injected_secrets_it_uses_the_cluster(self, tmp_path: Path) -> None:
        settings = CellSettings("c", str(tmp_path / "c.db"), "t")
        with pytest.raises(TenantResourceError, match="service account"):
            create_app(settings)  # not running in a cluster


def test_owned_http_client_is_closed_on_shutdown(tmp_path: Path) -> None:
    app = create_app(CellSettings("c", str(tmp_path / "c.db"), "t"), secrets=MemorySecrets())
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    injected = StackHost().client()
    app = create_app(
        CellSettings("c", str(tmp_path / "c.db"), "t"),
        secrets=MemorySecrets(),
        http_client=injected,
    )
    with TestClient(app):
        pass
    assert not injected.is_closed  # the caller owns an injected client


def test_deleting_a_malformed_tenant_id_is_a_no_op(cell: Cell) -> None:
    response = cell.client.delete("/internal/tenants/Not_Valid", headers=INTERNAL)
    assert response.status_code == 204


def test_registries_from_before_tenant_stacks_are_upgraded(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "provisioned_at TEXT NOT NULL, isolation TEXT NOT NULL DEFAULT 'pooled')"
        )
        conn.execute("INSERT INTO tenants VALUES ('acme', 'active', 'then', 'pooled')")
    store = CellStore(str(path))
    assert store.get_tenant("acme") == {
        "tenant_id": "acme",
        "status": "active",
        "provisioned_at": "then",
    }
    assert store.stack_token("acme") is None


def test_unfreezing_an_active_tenant_changes_nothing(cell: Cell) -> None:
    cell.onboard()
    response = cell.client.post("/internal/tenants/acme/unfreeze", headers=INTERNAL)
    assert (response.status_code, response.json()["status"]) == (200, "active")


def test_an_export_refused_by_the_data_layer_keeps_its_code(
    cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cellarch.data_plane.cell import app as gateway
    from cellarch.data_plane.cell.tenant_data import TenantDataError

    cell.onboard()
    cell.client.post("/internal/tenants/acme/freeze", headers=INTERNAL)

    def refused(*args: object, **kwargs: object) -> object:
        raise TenantDataError("invalid_export", "a row cannot be exported", 422)

    monkeypatch.setattr(gateway, "export_tenant", refused)
    response = cell.client.get("/internal/tenants/acme/export", headers=INTERNAL)
    assert (response.status_code, response.json()["error"]) == (422, "invalid_export")


class TestGraphqlForwarding:
    """The gateway's /graphql: the cell's mgr, for tenants the cell hosts."""

    def ask(self, cell: Cell, method: str = "GET") -> Any:
        return cell.client.request(
            method,
            "/graphql",
            params={"query": "{ me { tenantId } }"},
            headers={TENANT_HEADER: "acme"},
        )

    def test_a_tenant_being_set_up_is_not_ready(self, cell: Cell) -> None:
        cell.provision()
        response = self.ask(cell)
        assert (response.status_code, response.json()["errors"][0]["extensions"]["code"]) == (
            503,
            "tenant_not_ready",
        )

    def test_a_frozen_tenant_can_only_read(self, cell: Cell) -> None:
        cell.onboard()
        cell.client.post("/internal/tenants/acme/freeze", headers=INTERNAL)
        response = self.ask(cell, "POST")
        extensions = response.json()["errors"][0]["extensions"]
        assert (response.status_code, extensions["code"], extensions["retryAfter"]) == (
            503,
            "tenant_migrating",
            1,
        )

    def test_an_unreachable_mgr_is_unavailable(self, cell: Cell) -> None:
        cell.onboard()  # this cell's mgr isn't running in the stack host
        response = self.ask(cell)
        assert response.json()["errors"][0]["extensions"]["code"] == "mgr_unavailable"

    def test_without_an_mgr_there_is_no_graphql(self, tmp_path: Path) -> None:
        bare = make_cell(tmp_path / "bare", mgr=False)
        bare.onboard()
        assert bare.client.get("/graphql", headers={TENANT_HEADER: "acme"}).status_code == 404
