"""Stage 2b acceptance tests: usage metering and the cell request cap (A38-A39)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.headers import CELL_HEADER, INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.data_plane.cell.app import CellSettings, create_app
from tests.fakes import MemorySecrets, StackHost, stack_url_template

from .conftest import ADMIN_TOKEN, METRICS_TOKEN, Platform


def test_a38_router_meters_usage_per_tenant(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.onboard("globex")
    platform.set_limit("acme", rps=0.001, burst=3)
    platform.as_tenant("acme", "GET", "/v1/users")  # 200
    platform.as_tenant("acme", "GET", "/v1/users")  # 200
    platform.as_tenant("acme", "GET", "/v1/users/usr_missing")  # 404 from its stack
    platform.as_tenant("acme", "GET", "/v1/users")  # 429: burst used up
    platform.as_tenant("globex", "POST", "/v1/users", json={"email": "g@g.test", "name": "G"})
    platform.as_tenant("nobody", "GET", "/v1/users")  # unknown: not tracked

    without_token = platform.router.get("/_router/stats")
    wrong_token = platform.router.get(
        "/_router/stats", headers={INTERNAL_TOKEN_HEADER: ADMIN_TOKEN}
    )
    stats = platform.router.get(
        "/_router/stats", headers={INTERNAL_TOKEN_HEADER: METRICS_TOKEN}
    ).json()

    assert (without_token.status_code, wrong_token.status_code) == (401, 401)
    usage = {t["tenant_id"]: t for t in stats["tenants"]}
    assert set(usage) == {"acme", "globex"}
    assert usage["acme"]["requests"] == 4  # everything the tenant sent
    assert usage["acme"]["throttled"] == 1  # never reached the cell
    assert usage["acme"]["responses"] == {"2xx": 2, "3xx": 0, "4xx": 2, "5xx": 0}
    assert usage["globex"]["requests"] == 1
    assert usage["globex"]["responses"]["2xx"] == 1
    assert stats["tenants"][0]["tenant_id"] == "acme"  # busiest first
    assert stats["router_replicas"] == 1


def test_a39_an_overloaded_cell_rejects_new_requests_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stacks, secrets = StackHost(), MemorySecrets()
    app = create_app(
        CellSettings(
            "cell-x",
            str(tmp_path / "cell.db"),
            "cell-token",
            tenant_db_admin_url=f"sqlite:///{tmp_path.as_posix()}/tenants",
            tenant_service_url=stack_url_template("cell-x"),
            max_concurrent_requests=1,
        ),
        secrets=secrets,
        http_client=stacks.client(),
    )
    internal = {INTERNAL_TOKEN_HEADER: "cell-token"}
    setup = TestClient(app)
    setup.post("/internal/tenants", json={"tenant_id": "acme"}, headers=internal)
    stacks.start("cell-x", "acme", secrets.values["acme"])
    assert setup.post("/internal/tenants/acme/activate", headers=internal).status_code == 200
    started, release = threading.Event(), threading.Event()
    original = app.state.store.get_tenant

    def slow_get_tenant(*args: Any, **kwargs: Any) -> Any:
        started.set()
        release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.store, "get_tenant", slow_get_tenant)

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://cell-x") as client:
            busy = asyncio.create_task(client.get("/v1/users", headers={TENANT_HEADER: "acme"}))
            await asyncio.to_thread(started.wait, 5)
            rejected = await client.get("/v1/users", headers={TENANT_HEADER: "acme"})
            health = await client.get("/healthz")
            release.set()
            return await busy, rejected, health

    completed, rejected, health = asyncio.run(scenario())

    assert completed.status_code == 200
    assert (rejected.status_code, rejected.json()["error"]) == (503, "cell_overloaded")
    assert rejected.headers["Retry-After"] == "1"
    assert rejected.headers[CELL_HEADER] == "cell-x"
    assert health.status_code == 200
