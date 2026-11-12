from __future__ import annotations

import httpx
import pytest

from demo import DemoError, run_demo

from .conftest import Platform


def test_demo_runs_end_to_end(platform: Platform, capsys: pytest.CaptureFixture[str]) -> None:
    platform.register_cell("cell-1", capacity=4)
    platform.register_cell("cell-2", capacity=4)

    run_demo(platform.admin, platform.router)

    out = capsys.readouterr().out
    assert "acme" in out and "umbrella" in out
    assert "cell-1" in out and "cell-2" in out  # fill-first moved on to cell-2
    assert "Demo complete" in out


def test_demo_can_run_again(platform: Platform) -> None:
    platform.register_cell("cell-1", capacity=4)
    platform.register_cell("cell-2", capacity=4)
    run_demo(platform.admin, platform.router)
    run_demo(platform.admin, platform.router)


def test_demo_fails_loudly_when_a_check_fails(platform: Platform) -> None:
    def broken_router(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom", "message": "broken"})

    router = httpx.Client(transport=httpx.MockTransport(broken_router), base_url="http://router")
    platform.register_cell("cell-1", capacity=4)
    platform.register_cell("cell-2", capacity=4)

    with pytest.raises(DemoError):
        run_demo(platform.admin, router)
