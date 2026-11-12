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
# Credentials the control plane is configured with (environment).
CELL_TOKENS = {"cell-1": "test-cell-1-token", "cell-2": "test-cell-2-token"}
# Running cells the control plane has no credential for yet (registered via the API).
UNCONFIGURED_CELL_TOKENS = {"cell-3": "test-cell-3-token"}
# Address slots with nothing running; a fake Docker runner "starts" cells here.
SPARE_CELL_IDS = ("cell-4",)
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


async def _nothing_running(scope: Scope, receive: Receive, send: Send) -> None:
    raise httpx.ConnectError("nothing is running at this address")


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
    tmp_path: Path
    router_mounts: dict[str, httpx.AsyncBaseTransport]

    def extra_router(self, **settings: Any) -> TestClient:
        """Another router on the same platform, e.g. one of several replicas."""
        app = create_router(
            RouterSettings(
                control_plane_url=CONTROL_PLANE_URL,
                router_token=ROUTER_TOKEN,
                cache_ttl_seconds=ROUTE_CACHE_TTL,
                **settings,
            ),
            http_client=httpx.AsyncClient(mounts=self.router_mounts),
            clock=self.clock,
        )
        return TestClient(app, base_url="http://router-2")

    def set_limit(self, tenant_id: str, rps: float, burst: int) -> dict[str, Any]:
        response = self.admin.put(
            f"/v1/tenants/{tenant_id}/rate-limit", json={"rps": rps, "burst": burst}
        )
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    def start_cell(self, cell_id: str, token: str) -> None:
        """Start a real cell app in a spare address slot (what `docker compose up` does)."""
        settings = CellSettings(cell_id, str(self.tmp_path / cell_id / f"{cell_id}.db"), token)
        self.cells[cell_id].app = create_cell(settings)
        self.cells[cell_id].reachable = True

    def stop_cell(self, cell_id: str) -> None:
        self.cells[cell_id].app = _nothing_running
        self.cells[cell_id].reachable = False

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
                    db_path=str(tmp_path / cell_id / f"{cell_id}.db"),  # own folder, like a volume
                    internal_token=token,
                )
            )
        )
        for cell_id, token in {**CELL_TOKENS, **UNCONFIGURED_CELL_TOKENS}.items()
    }
    for cell_id in SPARE_CELL_IDS:
        cells[cell_id] = Switch(_nothing_running)
        cells[cell_id].reachable = False
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
    router_mounts: dict[str, httpx.AsyncBaseTransport] = {
        **cell_mounts,
        CONTROL_PLANE_URL: httpx.ASGITransport(app=control_plane),
    }
    router_app = create_router(
        RouterSettings(
            control_plane_url=CONTROL_PLANE_URL,
            router_token=ROUTER_TOKEN,
            cache_ttl_seconds=ROUTE_CACHE_TTL,
        ),
        http_client=httpx.AsyncClient(mounts=router_mounts),
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
            tmp_path=tmp_path,
            router_mounts=router_mounts,
        )
