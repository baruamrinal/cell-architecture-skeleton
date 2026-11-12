"""Who created and last changed cells and tenants (docs/acceptance-criteria.md A25-A26)."""

from __future__ import annotations

import pytest

from cellarch.common.headers import ACTOR_HEADER

from .conftest import Platform


def test_a25_cells_and_tenants_record_who_created_and_last_changed_them(
    platform: Platform,
) -> None:
    created = platform.admin.post(
        "/v1/cells",
        json={"cell_id": "cell-1", "url": "http://cell-1", "capacity": 4},
        headers={ACTOR_HEADER: "mrinal"},
    ).json()
    changed = platform.admin.patch(
        "/v1/cells/cell-1", json={"status": "draining"}, headers={ACTOR_HEADER: "ops-bot"}
    ).json()
    platform.set_cell_status("cell-1", "active")  # no actor name: role only
    anonymous = platform.admin.get("/v1/cells/cell-1").json()
    tenant = platform.admin.post(
        "/v1/tenants", json={"tenant_id": "acme", "name": "Acme"}, headers={ACTOR_HEADER: "mrinal"}
    ).json()

    assert (created["created_by"], created["updated_by"]) == ("admin:mrinal", "admin:mrinal")
    assert (changed["created_by"], changed["updated_by"]) == ("admin:mrinal", "admin:ops-bot")
    assert changed["updated_at"] >= created["updated_at"]
    assert anonymous["updated_by"] == "admin"
    assert (tenant["created_by"], tenant["updated_by"]) == ("admin:mrinal", "admin:mrinal")


@pytest.mark.parametrize("actor", ["has space", "a" * 65, "semi;colon", "über"])
def test_a26_actor_names_are_validated(platform: Platform, actor: str) -> None:
    response = platform.admin.get("/v1/cells", headers={ACTOR_HEADER: actor})

    assert (response.status_code, response.json()["error"]) == (400, "invalid_actor")
