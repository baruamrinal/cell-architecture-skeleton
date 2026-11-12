"""Cell lifecycle acceptance tests: register, manage, decommission, status, and the
`cellctl cells deploy/decommission` commands (docs/acceptance-criteria.md A16-A24).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from cellarch.cli.cellctl import main as cellctl_main
from cellarch.common.headers import CELL_HEADER

from .conftest import ADMIN_TOKEN, UNCONFIGURED_CELL_TOKENS, Platform

CELL_3_TOKEN = UNCONFIGURED_CELL_TOKENS["cell-3"]


def register(platform: Platform, **body: Any) -> Any:
    return platform.admin.post("/v1/cells", json={"capacity": 4, **body})


# --- API --------------------------------------------------------------------------------


def test_a16_a_running_cell_can_be_registered_with_its_credential(platform: Platform) -> None:
    response = register(platform, cell_id="cell-3", url="http://cell-3", credential=CELL_3_TOKEN)

    assert response.status_code == 201
    assert platform.onboard("acme") == "cell-3"
    served = platform.as_tenant("acme", "GET", "/v1/users")
    assert (served.status_code, served.headers[CELL_HEADER]) == (200, "cell-3")


@pytest.mark.parametrize(
    ("body", "make_unreachable", "error"),
    [
        (
            {"cell_id": "cell-3", "credential": "wrong-credential-value"},
            False,
            "cell_credential_rejected",
        ),
        ({"cell_id": "cell-9", "credential": CELL_3_TOKEN}, False, "cell_id_mismatch"),
        ({"cell_id": "cell-3", "credential": CELL_3_TOKEN}, True, "cell_unreachable"),
    ],
)
def test_a17_registration_verifies_the_cell_is_up_and_the_credential_works(
    platform: Platform, body: dict[str, str], make_unreachable: bool, error: str
) -> None:
    if make_unreachable:
        platform.cells["cell-3"].reachable = False

    response = register(platform, url="http://cell-3", **body)

    assert (response.status_code, response.json()["error"]) == (422, error)
    assert platform.admin.get(f"/v1/cells/{body['cell_id']}").status_code == 404


def test_a18_credentials_are_never_returned(platform: Platform) -> None:
    registered = register(platform, cell_id="cell-3", url="http://cell-3", credential=CELL_3_TOKEN)
    rejected = register(platform, cell_id="cell-5", url="http://cell-5", credential="tiny-secret")

    responses = [
        registered,
        platform.admin.get("/v1/cells/cell-3"),
        platform.admin.get("/v1/cells"),
        platform.admin.get("/v1/status"),
    ]
    assert all(r.status_code == 200 or r.status_code == 201 for r in responses)
    assert all(CELL_3_TOKEN not in r.text for r in responses)
    assert rejected.status_code == 422
    assert "tiny-secret" not in rejected.text  # validation errors don't echo input


def test_a19_capacity_can_be_changed_but_not_below_current_tenants(platform: Platform) -> None:
    platform.register_cell("cell-1", capacity=4)
    platform.onboard("acme")
    platform.onboard("globex")

    too_small = platform.admin.patch("/v1/cells/cell-1", json={"capacity": 1})
    raised = platform.admin.patch("/v1/cells/cell-1", json={"capacity": 10})

    assert (too_small.status_code, too_small.json()["error"]) == (409, "capacity_below_tenants")
    assert (raised.status_code, raised.json()["capacity"]) == (200, 10)


def test_a20_decommission_refuses_while_tenants_remain_and_forgets_the_cell(
    platform: Platform,
) -> None:
    platform.register_cell("cell-1")
    platform.onboard("acme")
    register(platform, cell_id="cell-3", url="http://cell-3", credential=CELL_3_TOKEN)

    refused = platform.admin.delete("/v1/cells/cell-1")
    removed = platform.admin.delete("/v1/cells/cell-3")

    assert (refused.status_code, refused.json()["error"]) == (409, "cell_not_empty")
    assert removed.status_code == 204
    assert platform.admin.get("/v1/cells/cell-3").status_code == 404
    assert [c["cell_id"] for c in platform.admin.get("/v1/cells").json()] == ["cell-1"]
    # Its credential was removed with it: re-registering needs the credential again.
    again = register(platform, cell_id="cell-3", url="http://cell-3")
    assert again.json()["error"] == "missing_cell_credential"


def test_a21_status_summarises_capacity_and_warns_before_it_runs_out(platform: Platform) -> None:
    platform.register_cell("cell-1", capacity=4)
    for n in range(1, 4):
        platform.onboard(f"tenant-{n}")

    past_target = platform.admin.get("/v1/status").json()
    platform.onboard("tenant-4")
    full = platform.admin.get("/v1/status").json()

    assert past_target["totals"] == {
        "cells": 1,
        "placeable_cells": 1,
        "tenants": 3,
        "capacity": 4,
        "free_slots": 1,
    }
    assert past_target["target_utilization"] == 0.75
    assert [w["code"] for w in past_target["warnings"]] == ["capacity_low"]
    assert [w["code"] for w in full["warnings"]] == ["no_capacity"]
    assert full["cells"][0]["utilization"] == 1.0


# --- cellctl cells deploy / decommission (cluster toolkit faked) ------------------------


class FakeClusters:
    """Stands in for k3d/Helm: installing the cell chart starts a real in-process cell."""

    def __init__(self, platform: Platform, *, gateway_comes_up: bool = True) -> None:
        self.platform = platform
        self.gateway_comes_up = gateway_comes_up
        self.existing: dict[str, str] = {"platform": "running"}
        self.calls: list[tuple[Any, ...]] = []

    def clusters(self) -> dict[str, str]:
        return dict(self.existing)

    def cell_ids(self) -> list[str]:
        return sorted(c for c in self.existing if c != "platform")

    def ensure_cluster(self, short: str, **kwargs: Any) -> None:
        self.calls.append(("cluster", short))
        self.existing[short] = "running"

    def import_images(self, short: str, images: Sequence[str]) -> None:
        self.calls.append(("import", short))

    def helm_upgrade(self, short: str, release: str, chart: Path, values: Any, **kw: Any) -> None:
        self.calls.append(("helm", short))
        if self.gateway_comes_up:
            self.platform.start_cell(values["cellId"], values["secrets"]["internalToken"])

    def helm_uninstall(self, short: str, release: str) -> None:
        self.calls.append(("uninstall", short, release))

    def ensure_network(self) -> None:
        pass

    def delete_network(self) -> None:
        pass

    def node_ip(self, short: str) -> str:
        return "172.20.0.9"

    def set_cell_dns(self, entries: Any) -> bool:
        self.calls.append(("dns", tuple(sorted(entries))))
        return True

    def stop(self, short: str) -> None:
        self.calls.append(("stop", short))
        self.existing[short] = "stopped"
        self.platform.stop_cell(short)

    def delete(self, short: str) -> None:
        self.calls.append(("delete", short))
        self.existing.pop(short, None)
        self.platform.stop_cell(short)


@pytest.fixture
def charts(tmp_path: Path) -> Path:
    (tmp_path / "charts" / "cell").mkdir(parents=True)
    return tmp_path / "charts"


def cellctl(platform: Platform, clusters: FakeClusters, charts: Path, *args: str) -> int:
    return cellctl_main(
        ["--charts-dir", str(charts), *args],
        client=platform.admin,
        token=ADMIN_TOKEN,
        clusters=clusters,
    )


def test_a22_cellctl_deploy_creates_a_cell_cluster_and_registers_it_in_one_step(
    platform: Platform, charts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clusters = FakeClusters(platform)

    code = cellctl(platform, clusters, charts, "cells", "deploy", "cell-4", "--capacity", "6")

    assert code == 0
    cell = json.loads(capsys.readouterr().out)
    assert (cell["cell_id"], cell["status"], cell["capacity"]) == ("cell-4", "active", 6)
    assert cell["url"] == "http://cell-4.cellarch.internal:8000"
    assert [c[0] for c in clusters.calls] == ["cluster", "import", "helm", "dns"]
    assert platform.onboard("acme") == "cell-4"
    assert platform.as_tenant("acme", "GET", "/v1/users").headers[CELL_HEADER] == "cell-4"


def test_a23_cellctl_deploy_cleans_up_when_the_cell_does_not_come_up(
    platform: Platform, charts: Path
) -> None:
    clusters = FakeClusters(platform, gateway_comes_up=False)

    code = cellctl(platform, clusters, charts, "cells", "deploy", "cell-4")

    assert code == 1
    assert ("delete", "cell-4") in clusters.calls
    assert "cell-4" not in clusters.existing
    assert clusters.calls[-1] == ("dns", ())
    assert platform.admin.get("/v1/cells/cell-4").status_code == 404


def test_a24_cellctl_decommission_deregisters_and_removes_the_cluster(
    platform: Platform, charts: Path
) -> None:
    clusters = FakeClusters(platform)
    assert cellctl(platform, clusters, charts, "cells", "deploy", "cell-4") == 0

    code = cellctl(platform, clusters, charts, "cells", "decommission", "cell-4", "--delete-data")

    assert code == 0
    assert platform.admin.get("/v1/cells/cell-4").status_code == 404
    assert ("delete", "cell-4") in clusters.calls
    assert not platform.cells["cell-4"].reachable
