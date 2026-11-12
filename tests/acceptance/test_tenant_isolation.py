"""Per-tenant data isolation tier (docs/acceptance-criteria.md A27-A30)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cellarch.common.headers import INTERNAL_TOKEN_HEADER

from .conftest import CELL_TOKENS, Platform


def shared_db(platform: Platform, cell_id: str) -> Path:
    return platform.tmp_path / cell_id / f"{cell_id}.db"


def tenant_db(platform: Platform, cell_id: str, tenant_id: str) -> Path:
    return platform.tmp_path / cell_id / "tenants" / f"{tenant_id}.db"


def item_names(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT name FROM items ORDER BY seq")]


def test_a27_an_isolated_tenants_data_lives_in_its_own_database(platform: Platform) -> None:
    platform.register_cell("cell-1")
    tenant = platform.create_tenant("acme", isolation="isolated").json()

    platform.as_tenant("acme", "POST", "/v1/items", json={"name": "acme secret"})

    assert tenant["isolation"] == "isolated"
    assert item_names(tenant_db(platform, "cell-1", "acme")) == ["acme secret"]
    assert item_names(shared_db(platform, "cell-1")) == []


def test_a28_pooled_and_isolated_tenants_on_one_cell_cannot_see_each_other(
    platform: Platform,
) -> None:
    platform.register_cell("cell-1")
    platform.create_tenant("acme", isolation="isolated")
    platform.create_tenant("globex", isolation="pooled")
    acme_item = platform.as_tenant("acme", "POST", "/v1/items", json={"name": "a"}).json()
    globex_item = platform.as_tenant("globex", "POST", "/v1/items", json={"name": "g"}).json()

    assert [i["name"] for i in platform.as_tenant("acme", "GET", "/v1/items").json()] == ["a"]
    assert [i["name"] for i in platform.as_tenant("globex", "GET", "/v1/items").json()] == ["g"]
    assert platform.as_tenant("globex", "GET", f"/v1/items/{acme_item['id']}").status_code == 404
    assert platform.as_tenant("acme", "GET", f"/v1/items/{globex_item['id']}").status_code == 404
    assert item_names(shared_db(platform, "cell-1")) == ["g"]


def test_a29_removing_an_isolated_tenant_deletes_its_database(platform: Platform) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    platform.as_tenant("acme", "POST", "/v1/items", json={"name": "a"})
    assert tenant_db(platform, "cell-1", "acme").exists()

    response = platform.cell_clients["cell-1"].delete(
        "/internal/tenants/acme", headers={INTERNAL_TOKEN_HEADER: CELL_TOKENS["cell-1"]}
    )

    assert response.status_code == 204
    assert not tenant_db(platform, "cell-1", "acme").exists()


def test_a30_tenants_are_isolated_by_default_and_the_value_is_validated(
    platform: Platform,
) -> None:
    platform.register_cell("cell-1", capacity=10)

    default = platform.create_tenant("acme").json()
    pooled = platform.create_tenant("globex", isolation="pooled").json()
    invalid = platform.create_tenant("initech", isolation="silo")
    status = platform.admin.get("/v1/status").json()

    assert default["isolation"] == "isolated"
    assert pooled["isolation"] == "pooled"
    assert (invalid.status_code, invalid.json()["error"]) == (422, "validation_error")
    assert status["totals"]["isolated_tenants"] == 1
    assert status["cells"][0]["isolated_tenants"] == 1
