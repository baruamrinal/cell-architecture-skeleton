from __future__ import annotations

from typing import Any

import httpx
import pytest

from demo import DemoError, run_demo

from .conftest import Platform


def onboard_with(platform: Platform) -> Any:
    def onboard(tenant_id: str) -> dict[str, Any]:
        platform.onboard(tenant_id)
        tenant: dict[str, Any] = platform.admin.get(f"/v1/tenants/{tenant_id}").json()
        return tenant

    return onboard


def test_demo_runs_end_to_end(platform: Platform, capsys: pytest.CaptureFixture[str]) -> None:
    platform.register_cell("cell-1", capacity=4)
    platform.register_cell("cell-2", capacity=4)

    run_demo(platform.admin, platform.router, onboard_with(platform))

    out = capsys.readouterr().out
    assert "acme" in out and "umbrella" in out
    assert "cell-1" in out and "cell-2" in out  # fill-first moved on to cell-2
    assert "Demo complete" in out


def test_demo_can_run_again(platform: Platform) -> None:
    platform.register_cell("cell-1", capacity=4)
    platform.register_cell("cell-2", capacity=4)
    run_demo(platform.admin, platform.router, onboard_with(platform))
    run_demo(platform.admin, platform.router, onboard_with(platform))


def test_demo_fails_loudly_when_a_check_fails(platform: Platform) -> None:
    def broken_router(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom", "message": "broken"})

    router = httpx.Client(transport=httpx.MockTransport(broken_router), base_url="http://router")
    platform.register_cell("cell-1", capacity=4)
    platform.register_cell("cell-2", capacity=4)

    with pytest.raises(DemoError):
        run_demo(platform.admin, router, onboard_with(platform))


def test_demo_refuses_tenants_left_half_onboarded(platform: Platform) -> None:
    platform.register_cell("cell-1", capacity=4)
    platform.create_tenant("acme")  # placed, but its stack never started
    with pytest.raises(DemoError, match="provisioning"):
        run_demo(platform.admin, platform.router, onboard_with(platform))
