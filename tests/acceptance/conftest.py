"""In-process wiring of the whole platform for acceptance tests.

control plane, router and two cells run as ASGI apps connected through httpx
ASGI transports: no Docker, no network, no sleeping (the router gets a fake clock).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from cellarch.common.headers import INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.control_plane.app import ControlPlaneSettings
from cellarch.control_plane.app import create_app as create_control_plane
from cellarch.data_plane.cell.app import CellSettings
from cellarch.data_plane.cell.app import create_app as create_cell
from cellarch.data_plane.router.app import RouterSettings
from cellarch.data_plane.router.app import create_app as create_router

ADMIN_TOKEN = "test-admin-token"
ROUTER_TOKEN = "test-router-token"
CELL_TOKENS = {"cell-1": "test-cell-1-token", "cell-2": "test-cell-2-token"}
CONTROL_PLANE_URL = "http://control-plane"
ROUTE_CACHE_TTL = 5.0


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]


class Switch:
    """ASGI wrapper that records requests and can simulate an outage."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.reachable = True
        self.requests: list[RecordedRequest] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if not self.reachable:
                raise httpx.ConnectError("simulated outage")
            self.requests.append(
                RecordedRequest(
                    method=scope["method"],
                    path=scope["path"],
                    headers={k.decode().lower(): v.decode() for k, v in scope["headers"]},
                )
            )
        await self.app(scope, receive, send)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Platform:
    admin: TestClient
    router: TestClient
    control_plane: Switch
    control_plane_app: Any
    cells: dict[str, Switch]
    cell_clients: dict[str, TestClient]
    clock: FakeClock

    def register_cell(self, cell_id: str, **fields: Any) -> dict[str, Any]:
        body = {"cell_id": cell_id, "url": f"http://{cell_id}", "capacity": 4, **fields}
        response = self.admin.post("/v1/cells", json=body)
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    def set_cell_status(self, cell_id: str, status: str) -> None:
        response = self.admin.patch(f"/v1/cells/{cell_id}", json={"status": status})
        assert response.status_code == 200, response.text

    def create_tenant(self, tenant_id: str, **fields: Any) -> httpx.Response:
        body = {"tenant_id": tenant_id, "name": tenant_id.title(), **fields}
        response: httpx.Response = self.admin.post("/v1/tenants", json=body)
        return response

    def onboard(self, tenant_id: str) -> str:
        """Create a tenant and return the cell it was placed on."""
        response = self.create_tenant(tenant_id)
        assert response.status_code == 201, response.text
        cell_id: str = response.json()["cell_id"]
        return cell_id

    def as_tenant(self, tenant_id: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {TENANT_HEADER: tenant_id, **kwargs.pop("headers", {})}
        response: httpx.Response = self.router.request(method, path, headers=headers, **kwargs)
        return response


@pytest.fixture
def platform(tmp_path: Path) -> Iterator[Platform]:
    cells = {
        cell_id: Switch(
            create_cell(
                CellSettings(
                    cell_id=cell_id,
                    db_path=str(tmp_path / f"{cell_id}.db"),
                    internal_token=token,
                )
            )
        )
        for cell_id, token in CELL_TOKENS.items()
    }
    cell_mounts: dict[str, httpx.AsyncBaseTransport] = {
        f"http://{cell_id}": httpx.ASGITransport(app=app) for cell_id, app in cells.items()
    }

    control_plane_app = create_control_plane(
        ControlPlaneSettings(
            db_path=str(tmp_path / "control-plane.db"),
            admin_token=ADMIN_TOKEN,
            router_token=ROUTER_TOKEN,
            cell_credentials=dict(CELL_TOKENS),
        ),
        http_client=httpx.AsyncClient(mounts=cell_mounts),
    )
    control_plane = Switch(control_plane_app)

    clock = FakeClock()
    router_app = create_router(
        RouterSettings(
            control_plane_url=CONTROL_PLANE_URL,
            router_token=ROUTER_TOKEN,
            cache_ttl_seconds=ROUTE_CACHE_TTL,
        ),
        http_client=httpx.AsyncClient(
            mounts={**cell_mounts, CONTROL_PLANE_URL: httpx.ASGITransport(app=control_plane)}
        ),
        clock=clock,
    )

    with (
        TestClient(
            control_plane, base_url=CONTROL_PLANE_URL, headers={INTERNAL_TOKEN_HEADER: ADMIN_TOKEN}
        ) as admin,
        TestClient(router_app, base_url="http://router") as router,
    ):
        yield Platform(
            admin=admin,
            router=router,
            control_plane=control_plane,
            control_plane_app=control_plane_app,
            cells=cells,
            cell_clients={
                cell_id: TestClient(app, base_url=f"http://{cell_id}")
                for cell_id, app in cells.items()
            },
            clock=clock,
        )
