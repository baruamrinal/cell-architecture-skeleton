"""Up-front checks of promote and drain (the workflows run end to end in tests/acceptance)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cellarch.cli.api import CommandError
from cellarch.cli.operations import dedicated_cell_id, drain, promote

from .test_cellctl_lifecycle import FakeClusters


class ScriptedApi:
    def __init__(self, tenants: list[dict[str, Any]]) -> None:
        self.tenants = {t["tenant_id"]: t for t in tenants}
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        if path == "/v1/tenants":
            return list(self.tenants.values())
        if path.startswith("/v1/tenants/"):
            return self.tenants[path.rsplit("/", 1)[-1]]
        return {"cell_id": "cell-1", "type": "shared", "status": "active"}


def tenant(tenant_id: str, **fields: str) -> dict[str, str]:
    return {
        "tenant_id": tenant_id,
        "cell_id": "cell-1",
        "status": "active",
        "placement_mode": "pooled",
        **fields,
    }


def test_the_dedicated_cell_is_named_after_its_tenant() -> None:
    assert dedicated_cell_id("acme") == "cell-acme"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"placement_mode": "dedicated"}, "tenant_already_dedicated"),
        ({"status": "migrating"}, "tenant_not_active"),
    ],
)
def test_promote_checks_the_tenant_before_deploying_anything(
    fields: dict[str, str], code: str
) -> None:
    api, clusters = ScriptedApi([tenant("acme", **fields)]), FakeClusters()
    with pytest.raises(CommandError) as raised:
        promote(api, clusters, Path("charts"), "acme", progress=lambda m: None)
    assert raised.value.code == code
    assert clusters.calls == []


def test_drain_refuses_a_cell_with_tenants_that_are_not_active() -> None:
    api = ScriptedApi([tenant("acme"), tenant("globex", status="provisioning")])
    with pytest.raises(CommandError, match="globex") as raised:
        drain(api, FakeClusters(), Path("charts"), "cell-1", progress=lambda m: None)
    assert raised.value.code == "tenant_not_active"
    assert ("PATCH", "/v1/cells/cell-1") not in api.calls
