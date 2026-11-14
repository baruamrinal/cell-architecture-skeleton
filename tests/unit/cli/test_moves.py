"""The tenant move steps run by cellctl (docs/plan.md §5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cellarch.cli.api import CommandError
from cellarch.cli.clusters import ClusterError
from cellarch.cli.moves import move, rollback

from .test_onboarding import RecordingClusters

CHARTS = Path("charts")
MOVE = {
    "move_id": "mv_1",
    "tenant_id": "acme",
    "source_cell_id": "cell-1",
    "target_cell_id": "cell-2",
    "state": "started",
    "cleanup_pending": None,
    "copied_rows": 3,
}


class ScriptedApi:
    """The control plane's move API; ``not_ready`` answers per step, ``fail`` one step."""

    def __init__(self, *, not_ready: dict[str, int] | None = None, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.not_ready = dict(not_ready or {})
        self.fail = fail
        self.move = dict(MOVE)
        self.cleanup_pending: str | None = None

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path, body))
        if path == "/v1/tenants/acme":
            return {"tenant_id": "acme", "tier": "premium", "cell_id": "cell-1"}
        step = path.rsplit("/", 1)[-1]
        if self.fail == step:
            raise CommandError("tenant_data_not_empty", "scripted refusal", 409)
        if self.not_ready.get(step):
            self.not_ready[step] -= 1
            raise CommandError("tenant_stack_not_ready", "not ready", 409)
        states = {
            "moves": "started",
            "copy": "copied",
            "switch": "switched",
            "complete": "completed",
            "rollback": "rolled_back",
        }
        if step in states:
            self.move["state"] = states[step]
        if step == "complete":
            self.move["cleanup_pending"] = self.cleanup_pending
        if step == "mv_1":  # GET /v1/moves/mv_1
            return dict(self.move)
        return dict(self.move)

    def steps(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for _, path, _ in self.calls]


def run(api: ScriptedApi, clusters: RecordingClusters, to: str | None = "cell-2") -> Any:
    messages: list[str] = []
    result = move(api, clusters, CHARTS, "acme", to, progress=messages.append, sleep=lambda s: None)
    return result, messages


CELLS = ("cell-1", "cell-2")


def test_runs_every_step_in_order() -> None:
    api, clusters = ScriptedApi(), RecordingClusters(cells=CELLS)
    finished, _ = run(api, clusters)
    assert finished["state"] == "completed"
    assert api.steps() == ["acme", "moves", "copy", "switch", "complete"]
    assert api.calls[1][2] == {"target_cell_id": "cell-2"}
    assert clusters.calls == [
        (
            "install",
            "cell-2",
            "tenant-acme",
            "tenant-stack",
            {"tenantId": "acme", "tier": "premium"},
        ),
        ("uninstall", "cell-1", "tenant-acme"),
    ]


def test_without_a_target_the_control_plane_chooses() -> None:
    api = ScriptedApi()
    run(api, RecordingClusters(cells=CELLS), to=None)
    assert api.calls[1][2] == {}


def test_copy_and_switch_wait_for_the_target_stack() -> None:
    api = ScriptedApi(not_ready={"copy": 2, "switch": 3})
    finished, _ = run(api, RecordingClusters(cells=CELLS))
    assert finished["state"] == "completed"
    assert api.steps().count("copy") == 3 and api.steps().count("switch") == 4


@pytest.mark.parametrize(
    ("api", "clusters", "error"),
    [
        (ScriptedApi(fail="copy"), RecordingClusters(cells=CELLS), CommandError),
        (ScriptedApi(not_ready={"switch": 99}), RecordingClusters(cells=CELLS), CommandError),
        (ScriptedApi(), RecordingClusters(cells=CELLS, fail="install"), ClusterError),
    ],
    ids=["copy-refused", "stack-never-ready", "install-fails"],
)
def test_a_failure_before_the_switch_rolls_back(
    api: ScriptedApi, clusters: RecordingClusters, error: type[Exception]
) -> None:
    with pytest.raises(error):
        run(api, clusters)
    assert api.steps()[-1] == "rollback"
    reason = api.calls[-1][2]
    assert reason is not None and reason["reason"]
    assert clusters.calls[-1] == ("uninstall", "cell-2", "tenant-acme")
    assert "complete" not in api.steps()


def test_rollback_carries_on_past_its_own_failures() -> None:
    api = ScriptedApi(fail="copy")
    clusters = RecordingClusters(cells=CELLS, fail="uninstall")
    with pytest.raises(CommandError, match="scripted refusal"):  # the original error
        run(api, clusters)
    assert api.steps()[-1] == "rollback"


def test_a_failed_rollback_says_how_to_retry() -> None:
    class RollbackFails(ScriptedApi):
        def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if path.endswith("/rollback"):
                raise CommandError("cell_unavailable", "source down", 502)
            return super().call(method, path, body)

    api = RollbackFails(fail="copy")
    messages: list[str] = []
    with pytest.raises(CommandError, match="scripted refusal"):
        move(
            api,
            RecordingClusters(cells=CELLS),
            CHARTS,
            "acme",
            "cell-2",
            progress=messages.append,
            sleep=lambda s: None,
        )
    assert any("cellctl moves rollback mv_1" in m for m in messages)


def test_after_the_switch_the_move_goes_forward_even_if_the_old_stack_stays() -> None:
    api = ScriptedApi()
    api.cleanup_pending = "cell-1"
    clusters = RecordingClusters(cells=CELLS, fail="uninstall")
    finished, messages = run(api, clusters)
    assert (finished["state"], finished["cleanup_pending"]) == ("completed", "cell-1")
    assert any("cleanup" in m for m in messages)


def test_cells_without_a_local_cluster_are_refused_before_anything_starts() -> None:
    api = ScriptedApi()
    with pytest.raises(CommandError) as raised:
        run(api, RecordingClusters(cells=("cell-1",)))
    assert raised.value.code == "cell_not_local"
    assert api.steps() == ["acme"]


def test_a_target_chosen_elsewhere_is_rolled_back_if_not_local() -> None:
    class ChoosesCell9(ScriptedApi):
        def __init__(self) -> None:
            super().__init__()
            self.move["target_cell_id"] = "cell-9"

    api = ChoosesCell9()
    with pytest.raises(CommandError, match="cell-9"):
        run(api, RecordingClusters(cells=CELLS), to=None)
    assert api.steps()[-1] == "rollback"


def test_rollback_command_uninstalls_the_target_stack_first() -> None:
    api = ScriptedApi()
    clusters = RecordingClusters(cells=CELLS)
    result = rollback(api, clusters, "mv_1", progress=lambda m: None)
    assert result["state"] == "rolled_back"
    assert clusters.calls == [("uninstall", "cell-2", "tenant-acme")]
    assert api.steps() == ["mv_1", "rollback"]


def test_rollback_command_on_a_finished_move_touches_no_cluster() -> None:
    api = ScriptedApi()
    api.move["state"] = "rolled_back"
    clusters = RecordingClusters(cells=CELLS)
    rollback(api, clusters, "mv_1", progress=lambda m: None)
    assert clusters.calls == []
