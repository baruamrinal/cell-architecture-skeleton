from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.headers import INTERNAL_TOKEN_HEADER
from cellarch.control_plane.app import ControlPlaneSettings, create_app
from cellarch.control_plane.cell_client import CellClient, CellVerificationError
from cellarch.control_plane.credentials import LocalCredentialStore
from cellarch.control_plane.store import ControlPlaneStore
from tests.fakes import async_client

ADMIN = "admin-t"
CELL_3_TOKEN = "cell-3-credential-0123"


class TestLocalCredentialStore:
    def test_static_credentials_are_a_fallback(self, tmp_path: Path) -> None:
        store = LocalCredentialStore(str(tmp_path / "cp.db"), {"cell-1": "from-env"})
        assert store.get("cell-1") == "from-env"
        assert store.get("cell-2") is None

    def test_registered_credentials_take_precedence_and_persist(self, tmp_path: Path) -> None:
        db = str(tmp_path / "cp.db")
        LocalCredentialStore(db, {"cell-1": "from-env"}).put("cell-1", "registered")
        LocalCredentialStore(db, {}).put("cell-2", "first")
        reopened = LocalCredentialStore(db, {"cell-1": "from-env"})
        reopened.put("cell-2", "second")
        assert reopened.get("cell-1") == "registered"
        assert reopened.get("cell-2") == "second"

    def test_delete_removes_only_registered_credentials(self, tmp_path: Path) -> None:
        store = LocalCredentialStore(str(tmp_path / "cp.db"), {"cell-1": "from-env"})
        store.put("cell-1", "registered")
        store.put("cell-3", "registered-3")
        store.delete("cell-1")
        store.delete("cell-3")
        store.delete("cell-3")
        assert store.get("cell-1") == "from-env"
        assert store.get("cell-3") is None


class TestRetiredCells:
    def test_deleted_seed_cells_are_not_seeded_again(self, tmp_path: Path) -> None:
        store = ControlPlaneStore(str(tmp_path / "cp.db"))
        store.seed_cell("cell-1", "http://cell-1", capacity=4)
        store.delete_cell("cell-1")
        store.seed_cell("cell-1", "http://cell-1", capacity=4)
        assert store.get_cell("cell-1") is None

    def test_registering_again_clears_the_tombstone(self, tmp_path: Path) -> None:
        store = ControlPlaneStore(str(tmp_path / "cp.db"))
        store.seed_cell("cell-1", "http://cell-1", capacity=4)
        store.delete_cell("cell-1")
        store.add_cell(
            cell_id="cell-1",
            url="http://cell-1",
            type="shared",
            hosting="vendor",
            size="small",
            capacity=4,
            wave=1,
        )
        store.delete_cell("cell-1")
        store.add_cell(
            cell_id="cell-1",
            url="http://cell-1",
            type="shared",
            hosting="vendor",
            size="small",
            capacity=4,
            wave=1,
        )
        assert store.get_cell("cell-1") is not None


Handler = Callable[[httpx.Request], httpx.Response]


def verify(handler: Handler, cell_id: str = "cell-3", token: str = CELL_3_TOKEN) -> None:
    client = CellClient(async_client(transport=httpx.MockTransport(handler)), {})
    asyncio.run(client.verify(cell_id, "http://cell-3:8000", token))


def healthy(cell_id: str = "cell-3", internal_status: int = 200) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "cell_id": cell_id})
        return httpx.Response(internal_status, json=[])

    return handler


class TestVerify:
    def test_healthy_cell_with_working_credential(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return healthy()(request)

        verify(handler)
        assert [r.url.path for r in seen] == ["/healthz", "/internal/tenants"]
        assert seen[1].headers[INTERNAL_TOKEN_HEADER] == CELL_3_TOKEN

    @pytest.mark.parametrize(
        ("handler", "code"),
        [
            (healthy(cell_id="cell-7"), "cell_id_mismatch"),
            (healthy(internal_status=401), "cell_credential_rejected"),
            (healthy(internal_status=403), "cell_credential_rejected"),
            (healthy(internal_status=500), "cell_unreachable"),
            (lambda r: httpx.Response(503), "cell_unreachable"),
            (lambda r: httpx.Response(200, content=b"not json"), "cell_id_mismatch"),
        ],
    )
    def test_failures(self, handler: Handler, code: str) -> None:
        with pytest.raises(CellVerificationError) as exc:
            verify(handler)
        assert exc.value.code == code
        assert CELL_3_TOKEN not in exc.value.message

    @pytest.mark.parametrize("failing_path", ["/healthz", "/internal/tenants"])
    def test_connection_errors_are_unreachable(self, failing_path: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == failing_path:
                raise httpx.ConnectError("refused")
            return healthy()(request)

        with pytest.raises(CellVerificationError) as exc:
            verify(handler)
        assert exc.value.code == "cell_unreachable"


def fake_cells(tokens: dict[str, str]) -> Handler:
    """A MockTransport handler that behaves like healthy cells with the given tokens."""

    def handler(request: httpx.Request) -> httpx.Response:
        cell_id = request.url.host
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "cell_id": cell_id})
        if request.headers.get(INTERNAL_TOKEN_HEADER) != tokens.get(cell_id):
            return httpx.Response(401, json={"error": "unauthenticated"})
        if request.method == "POST":
            return httpx.Response(201, json={})
        return httpx.Response(200, json=[])

    return handler


def control_plane(tmp_path: Path, cell_tokens: dict[str, str]) -> TestClient:
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"),
            admin_token=ADMIN,
            router_token="router-t",
            cell_credentials={},
        ),
        http_client=async_client(transport=httpx.MockTransport(fake_cells(cell_tokens))),
    )
    return TestClient(app, headers={INTERNAL_TOKEN_HEADER: ADMIN})


class TestLifecycleApi:
    def test_registered_credential_survives_a_restart(self, tmp_path: Path) -> None:
        tokens = {"cell-3": CELL_3_TOKEN}
        first = control_plane(tmp_path, tokens)
        body = {"cell_id": "cell-3", "url": "http://cell-3", "credential": CELL_3_TOKEN}
        assert first.post("/v1/cells", json=body).status_code == 201

        restarted = control_plane(tmp_path, tokens)
        created = restarted.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        assert (created.status_code, created.json()["cell_id"]) == (201, "cell-3")

    def test_duplicate_is_rejected_before_contacting_the_cell(self, tmp_path: Path) -> None:
        client = control_plane(tmp_path, {"cell-3": CELL_3_TOKEN})
        body = {"cell_id": "cell-3", "url": "http://cell-3", "credential": CELL_3_TOKEN}
        client.post("/v1/cells", json=body)
        duplicate = client.post("/v1/cells", json={**body, "credential": "another-long-value"})
        assert (duplicate.status_code, duplicate.json()["error"]) == (409, "cell_exists")

    def test_delete_unknown_cell(self, tmp_path: Path) -> None:
        response = control_plane(tmp_path, {}).delete("/v1/cells/nope")
        assert (response.status_code, response.json()["error"]) == (404, "cell_not_found")

    def test_status_with_no_cells(self, tmp_path: Path) -> None:
        status = control_plane(tmp_path, {}).get("/v1/status").json()
        assert status["totals"] == {
            "cells": 0,
            "placeable_cells": 0,
            "tenants": 0,
            "capacity": 0,
            "free_slots": 0,
        }
        assert [w["code"] for w in status["warnings"]] == ["no_capacity"]

    def test_status_ignores_cells_that_take_no_new_tenants(self, tmp_path: Path) -> None:
        client = control_plane(tmp_path, {"cell-3": CELL_3_TOKEN})
        body = {"cell_id": "cell-3", "url": "http://cell-3", "credential": CELL_3_TOKEN}
        client.post("/v1/cells", json={**body, "capacity": 10})
        client.patch("/v1/cells/cell-3", json={"status": "draining"})
        totals = client.get("/v1/status").json()["totals"]
        assert (totals["cells"], totals["placeable_cells"], totals["capacity"]) == (1, 0, 0)

    def test_healthy_status_has_no_warnings(self, tmp_path: Path) -> None:
        client = control_plane(tmp_path, {"cell-3": CELL_3_TOKEN})
        body = {"cell_id": "cell-3", "url": "http://cell-3", "credential": CELL_3_TOKEN}
        client.post("/v1/cells", json={**body, "capacity": 10})
        assert client.get("/v1/status").json()["warnings"] == []
