from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cellarch.cli.cellctl import main
from cellarch.common.headers import INTERNAL_TOKEN_HEADER

STATS = {
    "since": "2026-11-12T10:00:00+00:00",
    "router_replicas": 1,
    "tenants": [
        {
            "tenant_id": "acme",
            "requests": 120,
            "throttled": 30,
            "responses": {"2xx": 85, "3xx": 0, "4xx": 34, "5xx": 1},
        },
        {
            "tenant_id": "globex",
            "requests": 4,
            "throttled": 0,
            "responses": {"2xx": 4, "3xx": 0, "4xx": 0, "5xx": 0},
        },
    ],
}


@pytest.fixture(autouse=True)
def no_metrics_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CELLCTL_METRICS_TOKEN", "CELLCTL_ROUTER_URL", "CELLCTL_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def router(seen: list[httpx.Request], payload: object = STATS) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://router")


def test_usage_shows_a_table_and_needs_no_admin_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[httpx.Request] = []
    code = main(["--metrics-token", "m", "usage"], router_client=router(seen))
    assert code == 0
    assert (seen[0].url.path, seen[0].headers[INTERNAL_TOKEN_HEADER]) == ("/_router/stats", "m")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Usage since 2026-11-12T10:00:00+00:00")
    assert lines[2].split() == ["TENANT", "REQUESTS", "THROTTLED", "2XX", "4XX", "5XX"]
    assert lines[3].split() == ["acme", "120", "30", "(25%)", "85", "34", "1"]
    assert lines[4].split() == ["globex", "4", "0", "(0%)", "4", "0", "0"]


def test_usage_with_no_traffic(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"since": "x", "router_replicas": 1, "tenants": []}
    assert main(["--metrics-token", "m", "usage"], router_client=router([], payload)) == 0
    assert "No tenant traffic yet." in capsys.readouterr().out


def test_metrics_token_and_router_url_from_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text(
        "ROUTER_METRICS_TOKEN=from-dotenv\nCELLCTL_ROUTER_URL=http://router.example:9090\n",
        encoding="utf-8",
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=STATS)

    monkeypatch.setattr(
        "cellarch.cli.cellctl.make_client",
        lambda url: httpx.Client(transport=httpx.MockTransport(handler), base_url=url),
    )
    assert main(["usage"]) == 0
    assert str(seen[0].url) == "http://router.example:9090/_router/stats"
    assert seen[0].headers[INTERNAL_TOKEN_HEADER] == "from-dotenv"


def test_usage_requires_a_metrics_token(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["usage"]) == 2
    assert "ROUTER_METRICS_TOKEN" in capsys.readouterr().err


def test_usage_reports_router_errors(capsys: pytest.CaptureFixture[str]) -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthenticated", "message": "bad"})

    client = httpx.Client(transport=httpx.MockTransport(unauthorized), base_url="http://router")
    assert main(["--metrics-token", "wrong", "usage"], router_client=client) == 1
    assert "unauthenticated" in capsys.readouterr().err
