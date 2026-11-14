"""cellctl tenants move, moves and audit, against a control plane with scripted cells."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.cli.cellctl import main
from cellarch.control_plane.app import ControlPlaneSettings, create_app

from ..control_plane.test_moves import ADMIN, ScriptedCells, cell_error
from .test_onboarding import RecordingClusters

TOKEN = ADMIN["X-Internal-Token"]


@pytest.fixture
def cells() -> ScriptedCells:
    return ScriptedCells()


@pytest.fixture
def control_plane(tmp_path: Path, cells: ScriptedCells) -> Iterator[TestClient]:
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"),
            admin_token=TOKEN,
            router_token="router-t",
            cell_credentials={"cell-1": "cell-1-credential", "cell-2": "cell-2-credential"},
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(cells.handler)),
    )
    with TestClient(app, base_url="http://cp", headers=ADMIN) as client:
        for cell_id in ("cell-1", "cell-2"):
            client.post("/v1/cells", json={"cell_id": cell_id, "url": f"http://{cell_id}"})
        client.post("/v1/tenants", json={"tenant_id": "acme", "name": "Acme"})
        client.post("/v1/tenants/acme/activate")
        yield client


def run(client: TestClient, *args: str) -> int:
    clusters = RecordingClusters(cells=("cell-1", "cell-2"))
    return main([*args], client=client, token=TOKEN, clusters=clusters, sleep=lambda s: None)  # type: ignore[arg-type]


def test_tenants_move(control_plane: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(control_plane, "tenants", "move", "acme", "--to", "cell-2") == 0
    captured = capsys.readouterr()
    finished = json.loads(captured.out)
    assert (finished["state"], finished["target_cell_id"]) == ("completed", "cell-2")
    assert "Moving acme from cell-1 to cell-2" in captured.err


def test_a_failed_move_is_reported(
    control_plane: TestClient, cells: ScriptedCells, capsys: pytest.CaptureFixture[str]
) -> None:
    cells.answers[("POST", "cell-2", "import")] = cell_error(409, "schema_version_mismatch")
    assert run(control_plane, "tenants", "move", "acme", "--to", "cell-2") == 1
    assert "error: schema_version_mismatch" in capsys.readouterr().err
    state = control_plane.get("/v1/moves").json()["items"][0]["state"]
    assert state == "rolled_back"


def test_moves_list_show_rollback_and_complete(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    started = control_plane.post("/v1/tenants/acme/moves", json={"target_cell_id": "cell-2"})
    move_id = started.json()["move_id"]

    assert run(control_plane, "moves", "list", "--tenant", "acme") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["MOVE", "TENANT", "FROM", "TO", "STATE", "CLEANUP", "UPDATED"]
    assert lines[1].split()[:6] == [move_id, "acme", "cell-1", "cell-2", "started", "-"]

    assert run(control_plane, "moves", "show", move_id) == 0
    assert json.loads(capsys.readouterr().out)["move_id"] == move_id

    assert run(control_plane, "moves", "rollback", move_id) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "rolled_back"

    assert run(control_plane, "moves", "complete", move_id) == 1
    assert "move_state_conflict" in capsys.readouterr().err


def test_moves_list_when_there_are_none(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(control_plane, "moves", "list") == 0
    assert capsys.readouterr().out.strip() == "No moves."


def test_audit(control_plane: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(control_plane, "audit", "--limit", "2") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["TIME", "ACTOR", "ACTION", "OUTCOME", "TENANT", "CELL", "ERROR"]
    assert [line.split()[2] for line in out[1:3]] == ["tenant.activate", "tenant.create"]
    assert out[-1].startswith("More: cellctl audit --cursor ")
    cursor = out[-1].split("--cursor ")[1].split()[0]

    assert run(control_plane, "audit", "--limit", "2", "--cursor", cursor) == 0
    older = capsys.readouterr().out.splitlines()
    assert [line.split()[2] for line in older[1:3]] == ["cell.register", "cell.register"]

    assert run(control_plane, "audit", "--tenant", "globex") == 0
    assert capsys.readouterr().out.strip() == "No audit events."
    assert run(control_plane, "audit", "--cell", "cell-2") == 0
    assert "cell.register" in capsys.readouterr().out
