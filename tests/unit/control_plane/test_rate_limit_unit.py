from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.config import ConfigError
from cellarch.common.contracts import RateLimit
from cellarch.common.headers import INTERNAL_TOKEN_HEADER
from cellarch.control_plane.app import ControlPlaneSettings, create_app, parse_rate_limit
from cellarch.control_plane.store import ControlPlaneStore
from tests.fakes import async_client

from .test_cell_lifecycle_unit import fake_cells


@pytest.mark.parametrize(
    ("raw", "limit"),
    [("50:100", RateLimit(rps=50, burst=100)), (" 0.5 : 1 ", RateLimit(rps=0.5, burst=1))],
)
def test_parse_rate_limit(raw: str, limit: RateLimit) -> None:
    assert parse_rate_limit("X", raw) == limit


@pytest.mark.parametrize("raw", ["50", "a:b", "0:10", "10:0", "-1:5", "10:1.5"])
def test_invalid_rate_limits(raw: str) -> None:
    with pytest.raises(ConfigError, match="RATE_LIMIT_STANDARD"):
        parse_rate_limit("RATE_LIMIT_STANDARD", raw)


def test_tier_limits_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_ADMIN_TOKEN", "a")
    monkeypatch.setenv("CONTROL_PLANE_ROUTER_TOKEN", "r")
    monkeypatch.setenv("RATE_LIMIT_STANDARD", "5:10")
    monkeypatch.delenv("RATE_LIMIT_PREMIUM", raising=False)
    settings = ControlPlaneSettings.from_env()
    assert settings.tier_limits["standard"] == RateLimit(rps=5, burst=10)
    assert settings.tier_limits["premium"] == RateLimit(rps=500, burst=1000)


def test_old_databases_get_override_columns(tmp_path: Path) -> None:
    db = tmp_path / "cp.db"
    ControlPlaneStore(str(db))
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tenants)")}
    assert {"rate_limit_rps", "rate_limit_burst"} <= columns


def client(tmp_path: Path) -> TestClient:
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"),
            admin_token="admin-t",
            router_token="router-t",
            cell_credentials={"cell-1": "c1"},
            seed_cells={"cell-1": "http://cell-1"},
        ),
        http_client=async_client(transport=httpx.MockTransport(fake_cells({"cell-1": "c1"}))),
    )
    return TestClient(app, headers={INTERNAL_TOKEN_HEADER: "admin-t"})


def test_routing_entry_carries_the_effective_limit(tmp_path: Path) -> None:
    cp = client(tmp_path)
    cp.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
    assert cp.get("/v1/routing/tenants/acme").json()["rate_limit"] == {"rps": 50, "burst": 100}
    cp.put("/v1/tenants/acme/rate-limit", json={"rps": 7, "burst": 9})
    assert cp.get("/v1/routing/tenants/acme").json()["rate_limit"] == {"rps": 7, "burst": 9}


def test_limit_changes_on_unknown_tenants(tmp_path: Path) -> None:
    cp = client(tmp_path)
    put = cp.put("/v1/tenants/nobody/rate-limit", json={"rps": 1, "burst": 1})
    delete = cp.delete("/v1/tenants/nobody/rate-limit")
    assert (put.status_code, put.json()["error"]) == (404, "tenant_not_found")
    assert (delete.status_code, delete.json()["error"]) == (404, "tenant_not_found")


@pytest.mark.parametrize("body", [{"rps": 0, "burst": 1}, {"rps": 1, "burst": 0}, {"rps": 1}])
def test_limit_values_are_validated(tmp_path: Path, body: dict[str, float]) -> None:
    cp = client(tmp_path)
    cp.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
    assert cp.put("/v1/tenants/acme/rate-limit", json=body).status_code == 422
