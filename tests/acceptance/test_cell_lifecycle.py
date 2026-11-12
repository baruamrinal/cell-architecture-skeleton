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
    served = platform.as_tenant("acme", "GET", "/v1/items")
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
        "isolated_tenants": 3,  # tenants are isolated by default
    }
    assert past_target["target_utilization"] == 0.75
    assert [w["code"] for w in past_target["warnings"]] == ["capacity_low"]
    assert [w["code"] for w in full["warnings"]] == ["no_capacity"]
    assert full["cells"][0]["utilization"] == 1.0


# --- cellctl cells deploy / decommission (Docker runner faked) ---------------------------


class FakeDocker:
    """Stands in for the Docker CLI: `up` starts the cell in the in-process platform."""

    def __init__(self, platform: Platform, *, container_starts: bool = True) -> None:
        self.platform = platform
        self.container_starts = container_starts
        self.calls: list[tuple[str, ...]] = []

    def compose(self, files: Sequence[Path], *args: str) -> None:
        self.calls.append(("compose", *args))
        cell_id = args[-1]
        if args[0] == "up" and self.container_starts:
            spec = json.loads(files[-1].read_text(encoding="utf-8"))
            environment = spec["services"][cell_id]["environment"]
            self.platform.start_cell(environment["CELL_ID"], environment["CELL_INTERNAL_TOKEN"])
        if args[0] == "rm":
            self.platform.stop_cell(cell_id)

    def volume_exists(self, name: str) -> bool:
        return False

    def remove_volume(self, name: str) -> None:
        self.calls.append(("volume-rm", name))


@pytest.fixture
def compose_file(tmp_path: Path) -> Path:
    path = tmp_path / "deploy" / "docker-compose.yml"
    path.parent.mkdir()
    path.write_text("name: cellarch\n", encoding="utf-8")
    return path


def cellctl(platform: Platform, docker: FakeDocker, compose_file: Path, *args: str) -> int:
    return cellctl_main(
        ["--compose-file", str(compose_file), *args],
        client=platform.admin,
        token=ADMIN_TOKEN,
        docker=docker,
    )


def test_a22_cellctl_deploy_starts_and_registers_a_cell_in_one_step(
    platform: Platform, compose_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docker = FakeDocker(platform)

    code = cellctl(platform, docker, compose_file, "cells", "deploy", "cell-4", "--capacity", "6")

    assert code == 0
    cell = json.loads(capsys.readouterr().out)
    assert (cell["cell_id"], cell["status"], cell["capacity"]) == ("cell-4", "active", 6)
    assert platform.onboard("acme") == "cell-4"
    assert platform.as_tenant("acme", "GET", "/v1/items").headers[CELL_HEADER] == "cell-4"
    spec = json.loads((compose_file.parent / "cells" / "cell-4.yml").read_text(encoding="utf-8"))
    token = spec["services"]["cell-4"]["environment"]["CELL_INTERNAL_TOKEN"]
    assert len(token) >= 32  # generated, not a hand-picked token
    assert token not in json.dumps(cell)


def test_a23_cellctl_deploy_cleans_up_when_the_cell_does_not_come_up(
    platform: Platform, compose_file: Path
) -> None:
    docker = FakeDocker(platform, container_starts=False)

    code = cellctl(platform, docker, compose_file, "cells", "deploy", "cell-4")

    assert code == 1
    assert any(call[:2] == ("compose", "rm") for call in docker.calls)
    assert ("volume-rm", "cellarch_cell-4-data") in docker.calls
    assert not (compose_file.parent / "cells" / "cell-4.yml").exists()
    assert platform.admin.get("/v1/cells/cell-4").status_code == 404


def test_a24_cellctl_decommission_deregisters_and_removes_the_container(
    platform: Platform, compose_file: Path
) -> None:
    docker = FakeDocker(platform)
    assert cellctl(platform, docker, compose_file, "cells", "deploy", "cell-4") == 0

    code = cellctl(
        platform, docker, compose_file, "cells", "decommission", "cell-4", "--delete-data"
    )

    assert code == 0
    assert platform.admin.get("/v1/cells/cell-4").status_code == 404
    assert ("volume-rm", "cellarch_cell-4-data") in docker.calls
    assert not platform.cells["cell-4"].reachable
    assert not (compose_file.parent / "cells" / "cell-4.yml").exists()
