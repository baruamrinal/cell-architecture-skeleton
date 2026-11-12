from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cellarch.common.headers import CELL_HEADER, INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.data_plane.cell.app import CellSettings, create_app
from cellarch.data_plane.cell.store import CellStore

TOKEN = "cell-token"


@pytest.fixture
def store(tmp_path: Path) -> CellStore:
    return CellStore(str(tmp_path / "cell.db"))


class TestCellStore:
    def test_provision_is_idempotent(self, store: CellStore) -> None:
        first = store.provision_tenant("acme")
        second = store.provision_tenant("acme")
        assert first == second
        assert first["status"] == "active"
        assert [t["tenant_id"] for t in store.list_tenants()] == ["acme"]

    def test_get_unknown_tenant_is_none(self, store: CellStore) -> None:
        assert store.get_tenant("nobody") is None

    def test_items_are_scoped_to_their_tenant(self, store: CellStore) -> None:
        store.provision_tenant("acme")
        store.provision_tenant("globex")
        item = store.create_item("acme", "a", {"k": 1})

        assert store.get_item("acme", item["id"]) == item
        assert store.get_item("globex", item["id"]) is None
        assert store.list_items("globex") == []
        assert store.delete_item("globex", item["id"]) is False
        assert store.delete_item("acme", item["id"]) is True
        assert store.get_item("acme", item["id"]) is None

    def test_list_items_in_creation_order_with_limit(self, store: CellStore) -> None:
        store.provision_tenant("acme")
        for name in ("one", "two", "three"):
            store.create_item("acme", name, {})
        assert [i["name"] for i in store.list_items("acme")] == ["one", "two", "three"]
        assert [i["name"] for i in store.list_items("acme", limit=2)] == ["one", "two"]

    def test_payload_round_trips(self, store: CellStore) -> None:
        store.provision_tenant("acme")
        item = store.create_item("acme", "x", {"nested": {"a": [1, 2]}})
        assert store.list_items("acme")[0]["payload"] == {"nested": {"a": [1, 2]}}
        assert item["tenant_id"] == "acme"

    def test_delete_tenant_removes_its_items(self, store: CellStore) -> None:
        store.provision_tenant("acme")
        item = store.create_item("acme", "x", {})
        assert store.delete_tenant("acme") is True
        assert store.get_tenant("acme") is None
        assert store.get_item("acme", item["id"]) is None
        assert store.delete_tenant("acme") is False


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(
        CellSettings(cell_id="cell-x", db_path=str(tmp_path / "c.db"), internal_token=TOKEN)
    )
    return TestClient(app)


def provision(client: TestClient, tenant_id: str = "acme") -> None:
    response = client.post(
        "/internal/tenants", json={"tenant_id": tenant_id}, headers={INTERNAL_TOKEN_HEADER: TOKEN}
    )
    assert response.status_code == 201


class TestCellApp:
    def test_healthz_reports_cell(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "cell_id": "cell-x"}
        assert response.headers[CELL_HEADER] == "cell-x"

    def test_missing_tenant_header(self, client: TestClient) -> None:
        response = client.get("/v1/items")
        assert (response.status_code, response.json()["error"]) == (400, "missing_tenant")

    def test_malformed_tenant_header(self, client: TestClient) -> None:
        response = client.get("/v1/items", headers={TENANT_HEADER: "Not Valid"})
        assert (response.status_code, response.json()["error"]) == (400, "invalid_tenant")

    def test_item_lifecycle(self, client: TestClient) -> None:
        provision(client)
        headers = {TENANT_HEADER: "acme"}
        created = client.post(
            "/v1/items", json={"name": "a", "payload": {"x": 1}}, headers=headers
        ).json()
        assert client.get(f"/v1/items/{created['id']}", headers=headers).json() == created
        assert client.delete(f"/v1/items/{created['id']}", headers=headers).status_code == 204
        missing = client.get(f"/v1/items/{created['id']}", headers=headers)
        assert (missing.status_code, missing.json()["error"]) == (404, "item_not_found")

    def test_item_validation(self, client: TestClient) -> None:
        provision(client)
        response = client.post("/v1/items", json={"name": ""}, headers={TENANT_HEADER: "acme"})
        assert (response.status_code, response.json()["error"]) == (422, "validation_error")

    def test_provision_rejects_invalid_tenant_id(self, client: TestClient) -> None:
        response = client.post(
            "/internal/tenants", json={"tenant_id": "../x"}, headers={INTERNAL_TOKEN_HEADER: TOKEN}
        )
        assert response.status_code == 422

    def test_internal_list_and_idempotent_delete(self, client: TestClient) -> None:
        provision(client)
        headers = {INTERNAL_TOKEN_HEADER: TOKEN}
        assert [
            t["tenant_id"] for t in client.get("/internal/tenants", headers=headers).json()
        ] == ["acme"]
        assert client.delete("/internal/tenants/acme", headers=headers).status_code == 204
        assert client.delete("/internal/tenants/acme", headers=headers).status_code == 204
        assert client.get("/internal/tenants", headers=headers).json() == []

    def test_settings_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CELL_ID", "cell-7")
        monkeypatch.setenv("CELL_DB_PATH", "/data/cell-7.db")
        monkeypatch.setenv("CELL_INTERNAL_TOKEN", "t")
        assert CellSettings.from_env() == CellSettings("cell-7", "/data/cell-7.db", "t")
