from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cellarch.data_plane.router.app import RouterSettings, create_app
from cellarch.data_plane.router.metering import Metering
from tests.fakes import async_client


def test_counts_requests_throttles_and_status_classes() -> None:
    metering = Metering()
    metering.record("acme", 200)
    metering.record("acme", 201)
    metering.record("acme", 302)
    metering.record("acme", 429, throttled=True)
    metering.record("acme", 502)
    metering.record("globex", 200)

    snapshot = metering.snapshot()

    assert [t["tenant_id"] for t in snapshot] == ["acme", "globex"]
    assert snapshot[0] == {
        "tenant_id": "acme",
        "requests": 5,
        "throttled": 1,
        "responses": {"2xx": 2, "3xx": 1, "4xx": 1, "5xx": 1},
    }


def test_ties_are_sorted_by_tenant_id() -> None:
    metering = Metering()
    metering.record("b", 200)
    metering.record("a", 200)
    assert [t["tenant_id"] for t in metering.snapshot()] == ["a", "b"]


def test_other_status_codes_are_counted_but_not_classified() -> None:
    metering = Metering()
    metering.record("acme", 101)
    assert metering.snapshot()[0]["requests"] == 1
    assert sum(metering.snapshot()[0]["responses"].values()) == 0


def test_stats_endpoint_is_off_without_a_metrics_token() -> None:
    app = create_app(
        RouterSettings(control_plane_url="http://cp", router_token="r"),
        http_client=async_client(),
    )
    response = TestClient(app).get("/_router/stats")
    assert (response.status_code, response.json()["error"]) == (404, "not_found")


def test_metrics_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp")
    monkeypatch.setenv("ROUTER_TOKEN", "r")
    monkeypatch.setenv("ROUTER_METRICS_TOKEN", "m")
    assert RouterSettings.from_env().metrics_token == "m"
    monkeypatch.delenv("ROUTER_METRICS_TOKEN")
    assert RouterSettings.from_env().metrics_token is None
