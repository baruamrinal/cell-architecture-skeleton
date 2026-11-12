"""Tenant onboarding and offboarding steps run by cellctl (docs/plan.md §3.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cellarch.cli.api import CommandError
from cellarch.cli.clusters import ClusterError
from cellarch.cli.onboarding import offboard, onboard, tenant_release

CHARTS = Path("charts")


class ScriptedApi:
    """The control plane's admin API with scripted failures."""

    def __init__(self, *, activate_failures: int = 0, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.activate_failures = activate_failures
        self.fail = fail
        self.tenants: dict[str, dict[str, Any]] = {}

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        if self.fail == f"{method} {path}":
            raise CommandError("cell_unavailable", "scripted failure", 502)
        if (method, path) == ("POST", "/v1/tenants"):
            assert body is not None
            if self.fail == "placement":
                raise CommandError("no_capacity", "no cell has room", 503)
            tenant = {**body, "cell_id": "cell-1", "status": "provisioning"}
            self.tenants[body["tenant_id"]] = tenant
            return tenant
        tenant_id = path.split("/")[3]
        if path.endswith("/activate"):
            if self.activate_failures:
                self.activate_failures -= 1
                raise CommandError("tenant_stack_not_ready", "not ready", 409)
            self.tenants[tenant_id]["status"] = "active"
            return self.tenants[tenant_id]
        if method == "DELETE":
            self.tenants.pop(tenant_id, None)
            return None
        return self.tenants[tenant_id]


class RecordingClusters:
    def __init__(self, cells: tuple[str, ...] = ("cell-1",), fail: str | None = None) -> None:
        self.cells = cells
        self.fail = fail
        self.calls: list[tuple[Any, ...]] = []

    def cell_ids(self) -> list[str]:
        return list(self.cells)

    def helm_upgrade(
        self, short: str, release: str, chart: Path, values: dict[str, Any], **kw: Any
    ) -> None:
        self.calls.append(("install", short, release, chart.name, values))
        if self.fail == "install":
            raise ClusterError("helm install failed")

    def helm_uninstall(self, short: str, release: str) -> None:
        self.calls.append(("uninstall", short, release))
        if self.fail == "uninstall":
            raise ClusterError("helm uninstall failed")


BODY = {"tenant_id": "acme", "name": "Acme", "tier": "premium"}


def run_onboard(api: ScriptedApi, clusters: RecordingClusters) -> dict[str, Any]:
    sleeps: list[float] = []
    result: dict[str, Any] = onboard(
        api, clusters, CHARTS, BODY, progress=lambda m: None, sleep=sleeps.append
    )
    return result


def test_places_installs_the_stack_and_activates() -> None:
    api, clusters = ScriptedApi(), RecordingClusters()
    tenant = run_onboard(api, clusters)
    assert tenant["status"] == "active"
    assert clusters.calls == [
        (
            "install",
            "cell-1",
            "tenant-acme",
            "tenant-stack",
            {"tenantId": "acme", "tier": "premium"},
        )
    ]
    assert api.calls == [("POST", "/v1/tenants"), ("POST", "/v1/tenants/acme/activate")]


def test_activation_is_retried_while_the_stack_starts() -> None:
    api = ScriptedApi(activate_failures=2)
    assert run_onboard(api, RecordingClusters())["status"] == "active"


def test_no_capacity_leaves_nothing_to_undo() -> None:
    api, clusters = ScriptedApi(fail="placement"), RecordingClusters()
    with pytest.raises(CommandError, match="no cell has room"):
        run_onboard(api, clusters)
    assert clusters.calls == []
    assert api.tenants == {}


@pytest.mark.parametrize(
    ("api", "clusters", "error"),
    [
        (ScriptedApi(), RecordingClusters(fail="install"), ClusterError),
        (ScriptedApi(activate_failures=99), RecordingClusters(), CommandError),
        (ScriptedApi(), RecordingClusters(cells=("cell-9",)), CommandError),
    ],
    ids=["stack-install-fails", "stack-never-ready", "cell-not-local"],
)
def test_any_failure_undoes_every_step(
    api: ScriptedApi, clusters: RecordingClusters, error: type[Exception]
) -> None:
    with pytest.raises(error):
        run_onboard(api, clusters)
    assert api.tenants == {}
    assert api.calls[-1] == ("DELETE", "/v1/tenants/acme")
    if "cell-1" in clusters.cells:
        assert clusters.calls[-1] == ("uninstall", "cell-1", "tenant-acme")


def test_undo_carries_on_past_its_own_failures() -> None:
    api = ScriptedApi(activate_failures=99, fail="DELETE /v1/tenants/acme")
    clusters = RecordingClusters(fail="uninstall")
    with pytest.raises(CommandError, match="not ready"):  # the original error is reported
        run_onboard(api, clusters)
    assert ("uninstall", "cell-1", "tenant-acme") in clusters.calls


def test_offboard_uninstalls_the_stack_then_deletes_the_tenant() -> None:
    api, clusters = ScriptedApi(), RecordingClusters()
    run_onboard(api, clusters)
    offboard(api, clusters, "acme", progress=lambda m: None)
    assert clusters.calls[-1] == ("uninstall", "cell-1", "tenant-acme")
    assert api.calls[-1] == ("DELETE", "/v1/tenants/acme")
    assert api.tenants == {}


def test_offboard_refuses_tenants_on_cells_it_cannot_reach() -> None:
    api = ScriptedApi()
    run_onboard(api, RecordingClusters())
    with pytest.raises(CommandError, match="cell-1"):
        offboard(api, RecordingClusters(cells=()), "acme", progress=lambda m: None)
    assert "acme" in api.tenants


def test_release_name() -> None:
    assert tenant_release("acme") == "tenant-acme"
