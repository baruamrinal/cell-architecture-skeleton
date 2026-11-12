from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.config import ConfigError
from cellarch.common.headers import INTERNAL_TOKEN_HEADER
from cellarch.control_plane.app import ControlPlaneSettings, create_app
from cellarch.control_plane.cell_client import CellClient, MissingCellCredentialError
from cellarch.control_plane.store import ControlPlaneStore, DuplicateError

from .test_cell_lifecycle_unit import fake_cells


def test_cell_client_refuses_cells_without_credentials() -> None:
    import asyncio

    client = CellClient(httpx.AsyncClient(), {"cell-1": "t"})
    assert client.has_credential("cell-1")
    assert not client.has_credential("cell-2")
    with pytest.raises(MissingCellCredentialError):
        asyncio.run(client.provision_tenant("cell-2", "http://cell-2", "acme"))


def test_owned_http_client_is_closed_on_shutdown(tmp_path: Path) -> None:
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"), admin_token="a", router_token="r", cell_credentials={}
        )
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


ADMIN = "admin-t"
ROUTER = "router-t"


@pytest.fixture
def store(tmp_path: Path) -> ControlPlaneStore:
    return ControlPlaneStore(str(tmp_path / "cp.db"))


def add_cell(store: ControlPlaneStore, cell_id: str = "cell-1", **fields: object) -> None:
    values: dict[str, object] = {
        "cell_id": cell_id,
        "url": f"http://{cell_id}",
        "type": "shared",
        "hosting": "vendor",
        "size": "small",
        "capacity": 4,
        "wave": 1,
    }
    values.update(fields)
    store.add_cell(**values)  # type: ignore[arg-type]


class TestStore:
    def test_cells_report_tenant_count(self, store: ControlPlaneStore) -> None:
        add_cell(store)
        store.add_tenant("acme", "Acme", "standard", "cell-1")
        cell = store.get_cell("cell-1")
        assert cell is not None
        assert cell["tenant_count"] == 1
        assert [c["cell_id"] for c in store.list_cells()] == ["cell-1"]

    def test_duplicate_cell_raises(self, store: ControlPlaneStore) -> None:
        add_cell(store)
        with pytest.raises(DuplicateError):
            add_cell(store)

    def test_seed_cell_does_not_overwrite(self, store: ControlPlaneStore) -> None:
        add_cell(store, capacity=9)
        store.seed_cell("cell-1", "http://other", capacity=1)
        store.seed_cell("cell-2", "http://cell-2", capacity=2)
        cell_1 = store.get_cell("cell-1")
        cell_2 = store.get_cell("cell-2")
        assert cell_1 is not None and cell_1["capacity"] == 9
        assert cell_2 is not None and cell_2["capacity"] == 2

    def test_update_cell(self, store: ControlPlaneStore) -> None:
        add_cell(store)
        updated = store.update_cell("cell-1", status="draining", capacity=8)
        assert updated is not None
        assert (updated["status"], updated["capacity"]) == ("draining", 8)
        unchanged = store.update_cell("cell-1")
        assert unchanged is not None and unchanged["status"] == "draining"
        assert store.update_cell("missing", status="active") is None

    def test_tenant_lifecycle(self, store: ControlPlaneStore) -> None:
        add_cell(store)
        add_cell(store, "cell-2")
        tenant = store.add_tenant("acme", "Acme", "premium", "cell-1")
        assert (tenant["status"], tenant["placement_mode"]) == ("provisioning", "pooled")
        with pytest.raises(DuplicateError):
            store.add_tenant("acme", "Acme", "premium", "cell-1")

        store.set_tenant_status("acme", "active")
        store.assign_tenant("acme", "cell-2")
        routing = store.routing_entry("acme")
        assert routing == {
            "tenant_id": "acme",
            "cell_id": "cell-2",
            "cell_url": "http://cell-2",
            "status": "active",
            "tier": "premium",
            "rate_limit_rps": None,  # no override: the tier default applies
            "rate_limit_burst": None,
        }
        assert [t["tenant_id"] for t in store.list_tenants()] == ["acme"]

        store.delete_tenant("acme")
        assert store.get_tenant("acme") is None
        assert store.routing_entry("acme") is None


def make_client(tmp_path: Path, transport: httpx.AsyncBaseTransport | None = None) -> TestClient:
    transport = transport or httpx.MockTransport(fake_cells({"cell-1": "c1", "cell-2": "c2"}))
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"),
            admin_token=ADMIN,
            router_token=ROUTER,
            cell_credentials={"cell-1": "c1", "cell-2": "c2"},
            seed_cells={"cell-2": "http://cell-2"},
            default_cell_capacity=7,
        ),
        http_client=httpx.AsyncClient(transport=transport),
    )
    return TestClient(app, headers={INTERNAL_TOKEN_HEADER: ADMIN})


class TestApp:
    def test_healthz_needs_no_credential(self, tmp_path: Path) -> None:
        client = make_client(tmp_path)
        assert client.get("/healthz", headers={INTERNAL_TOKEN_HEADER: ""}).json() == {
            "status": "ok"
        }

    def test_seed_cells_are_registered_with_default_capacity(self, tmp_path: Path) -> None:
        cells = make_client(tmp_path).get("/v1/cells").json()
        assert [(c["cell_id"], c["capacity"], c["utilization"]) for c in cells] == [
            ("cell-2", 7, 0.0)
        ]

    def test_register_get_and_patch_cell(self, tmp_path: Path) -> None:
        client = make_client(tmp_path)
        created = client.post("/v1/cells", json={"cell_id": "cell-1", "url": "http://cell-1"})
        assert created.status_code == 201
        assert created.json()["capacity"] == 7
        assert client.post("/v1/cells", json={"cell_id": "cell-1", "url": "http://cell-1"}).json()[
            "error"
        ] == ("cell_exists")
        assert client.get("/v1/cells/cell-1").json()["type"] == "shared"
        patched = client.patch("/v1/cells/cell-1", json={"status": "draining"})
        assert patched.json()["status"] == "draining"

    def test_unknown_cell_and_tenant(self, tmp_path: Path) -> None:
        client = make_client(tmp_path)
        assert client.get("/v1/cells/nope").json()["error"] == "cell_not_found"
        assert client.patch("/v1/cells/nope", json={}).json()["error"] == "cell_not_found"
        assert client.get("/v1/tenants/nope").json()["error"] == "tenant_not_found"
        routing = client.get("/v1/routing/tenants/nope")
        assert (routing.status_code, routing.json()["error"]) == (404, "tenant_not_found")

    def test_tenant_creation_with_generated_id(self, tmp_path: Path) -> None:
        def cell_ok(request: httpx.Request) -> httpx.Response:
            assert request.headers[INTERNAL_TOKEN_HEADER] == "c2"
            return httpx.Response(201, json={})

        client = make_client(tmp_path, httpx.MockTransport(cell_ok))
        created = client.post("/v1/tenants", json={"name": "Acme"})
        assert created.status_code == 201
        assert created.json()["tenant_id"].startswith("t-")
        duplicate = client.post(
            "/v1/tenants", json={"tenant_id": created.json()["tenant_id"], "name": "Acme"}
        )
        assert (duplicate.status_code, duplicate.json()["error"]) == (409, "tenant_exists")
        assert len(client.get("/v1/tenants").json()) == 1

    def test_cell_rejecting_provisioning_is_reported(self, tmp_path: Path) -> None:
        client = make_client(tmp_path, httpx.MockTransport(lambda r: httpx.Response(500)))
        response = client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        assert (response.status_code, response.json()["error"]) == (502, "provisioning_failed")
        assert client.get("/v1/tenants").json() == []

    def test_invalid_tenant_id_rejected(self, tmp_path: Path) -> None:
        response = make_client(tmp_path).post(
            "/v1/tenants", json={"tenant_id": "Bad_ID", "name": "x"}
        )
        assert response.status_code == 422


class TestSettings:
    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTROL_PLANE_ADMIN_TOKEN", "a")
        monkeypatch.setenv("CONTROL_PLANE_ROUTER_TOKEN", "r")
        monkeypatch.setenv("CELL_CREDENTIALS", "cell-1=x")
        monkeypatch.setenv("SEED_CELLS", "cell-1=http://cell-1:8000")
        monkeypatch.setenv("DEFAULT_CELL_CAPACITY", "50")
        monkeypatch.setenv("TARGET_UTILIZATION", "0.8")
        settings = ControlPlaneSettings.from_env()
        assert settings.cell_credentials == {"cell-1": "x"}
        assert settings.seed_cells == {"cell-1": "http://cell-1:8000"}
        assert (settings.default_cell_capacity, settings.target_utilization) == (50, 0.8)

    def test_seed_cell_without_credential_is_a_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTROL_PLANE_ADMIN_TOKEN", "a")
        monkeypatch.setenv("CONTROL_PLANE_ROUTER_TOKEN", "r")
        monkeypatch.setenv("CELL_CREDENTIALS", "")
        monkeypatch.setenv("SEED_CELLS", "cell-1=http://cell-1:8000")
        with pytest.raises(ConfigError, match="cell-1"):
            ControlPlaneSettings.from_env()


class FakeCellApi:
    """A cell's /internal API with scripted answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.activate_status = 200
        self.delete_status = 204

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.path.endswith("/activate"):
            body = {"error": "tenant_stack_not_ready"} if self.activate_status == 409 else {}
            return httpx.Response(self.activate_status, json=body)
        if request.method == "DELETE":
            return httpx.Response(self.delete_status)
        return httpx.Response(201, json={})


class TestTenantHandshake:
    @pytest.fixture
    def cell(self) -> FakeCellApi:
        return FakeCellApi()

    @pytest.fixture
    def client(self, tmp_path: Path, cell: FakeCellApi) -> TestClient:
        return make_client(tmp_path, httpx.MockTransport(cell.handler))

    def test_a_new_tenant_waits_for_its_stack(self, client: TestClient, cell: FakeCellApi) -> None:
        created = client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        assert (created.status_code, created.json()["status"]) == (201, "provisioning")
        assert "isolation" not in created.json()
        assert cell.calls == [("POST", "/internal/tenants")]
        routed = client.get("/v1/routing/tenants/acme").json()
        assert routed["status"] == "provisioning"

    def test_activation(self, client: TestClient, cell: FakeCellApi) -> None:
        client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        activated = client.post("/v1/tenants/acme/activate")
        assert (activated.status_code, activated.json()["status"]) == (200, "active")
        assert cell.calls[-1] == ("POST", "/internal/tenants/acme/activate")
        assert client.post("/v1/tenants/acme/activate").json()["status"] == "active"

    def test_activation_before_the_stack_is_ready(
        self, client: TestClient, cell: FakeCellApi
    ) -> None:
        client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        cell.activate_status = 409
        response = client.post("/v1/tenants/acme/activate")
        assert (response.status_code, response.json()["error"]) == (409, "tenant_stack_not_ready")
        assert client.get("/v1/tenants/acme").json()["status"] == "provisioning"

    def test_activation_when_the_cell_fails(self, client: TestClient, cell: FakeCellApi) -> None:
        client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        cell.activate_status = 500
        response = client.post("/v1/tenants/acme/activate")
        assert (response.status_code, response.json()["error"]) == (502, "cell_unavailable")

    def test_delete(self, client: TestClient, cell: FakeCellApi) -> None:
        client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        assert client.delete("/v1/tenants/acme").status_code == 204
        assert cell.calls[-1] == ("DELETE", "/internal/tenants/acme")
        assert client.get("/v1/tenants/acme").status_code == 404
        assert client.get("/v1/cells/cell-2").json()["tenant_count"] == 0

    def test_delete_keeps_the_tenant_when_the_cell_fails(
        self, client: TestClient, cell: FakeCellApi
    ) -> None:
        client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        cell.delete_status = 502
        response = client.delete("/v1/tenants/acme")
        assert (response.status_code, response.json()["error"]) == (502, "cell_unavailable")
        assert client.get("/v1/tenants/acme").status_code == 200

    @pytest.mark.parametrize("method", ["POST", "DELETE"])
    def test_unknown_tenants(self, client: TestClient, method: str) -> None:
        path = "/v1/tenants/nobody" + ("/activate" if method == "POST" else "")
        response = client.request(method, path)
        assert (response.status_code, response.json()["error"]) == (404, "tenant_not_found")


def test_activation_when_the_cell_is_unreachable(tmp_path: Path) -> None:
    def unreachable_on_activate(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/activate"):
            raise httpx.ConnectError("cell is down")
        return httpx.Response(201, json={})

    client = make_client(tmp_path, httpx.MockTransport(unreachable_on_activate))
    client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
    response = client.post("/v1/tenants/acme/activate")
    assert (response.status_code, response.json()["error"]) == (502, "cell_unavailable")
