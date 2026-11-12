"""The local cluster toolkit behind `make up` and `cellctl cells deploy` (k3d + Helm)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from cellarch.cli.clusters import (
    ClusterError,
    LocalClusters,
    Result,
    cell_url,
    context,
)


class FakeRunner:
    """Records commands; answers some of them from a script of canned outputs."""

    def __init__(self, outputs: dict[str, str] | None = None, fail: str | None = None) -> None:
        self.outputs = outputs or {}
        self.fail = fail
        self.calls: list[tuple[list[str], str | None]] = []
        self.files: dict[str, str] = {}

    def __call__(self, cmd: Sequence[str], stdin: str | None = None) -> Result:
        joined = " ".join(cmd)
        self.calls.append((list(cmd), stdin))
        for arg in cmd:  # capture helm values files before they are deleted
            if arg.endswith(".json") and Path(arg).exists():
                self.files[arg] = Path(arg).read_text(encoding="utf-8")
        if self.fail and self.fail in joined:
            return Result(1, "", f"{self.fail} went wrong\n")
        for key, output in self.outputs.items():
            if key in joined:
                return Result(0, output, "")
        return Result(0, "", "")

    def commands(self) -> list[str]:
        return [" ".join(cmd) for cmd, _ in self.calls]


def cluster_list(**states: bool) -> str:
    return json.dumps(
        [
            {"name": f"cellarch-{name}", "serversRunning": 1 if running else 0, "serversCount": 1}
            for name, running in states.items()
        ]
        + [{"name": "someone-else", "serversRunning": 1, "serversCount": 1}]
    )


def toolkit(runner: FakeRunner) -> LocalClusters:
    ports = iter(range(6600, 6700))
    return LocalClusters(run=runner, free_port=lambda: next(ports))


def test_naming() -> None:
    assert context("cell-1") == "k3d-cellarch-cell-1"
    assert cell_url("cell-1") == "http://cell-1.cellarch.internal:8000"


def test_lists_only_cellarch_clusters_and_their_state() -> None:
    runner = FakeRunner({"cluster list": cluster_list(platform=True, **{"cell-1": False})})
    clusters = toolkit(runner)
    assert clusters.clusters() == {"platform": "running", "cell-1": "stopped"}
    assert clusters.cell_ids() == ["cell-1"]


def test_ensure_cluster_creates_a_lean_cluster_on_the_private_network() -> None:
    runner = FakeRunner({"cluster list": "[]"})
    toolkit(runner).ensure_cluster("platform", ports=["127.0.0.1:8080:8080"])
    create = next(c for c in runner.commands() if "cluster create" in c)
    assert "cellarch-platform" in create
    assert "--network cellarch" in create
    assert "--no-lb" in create
    assert "--api-port 127.0.0.1:6600" in create  # never host.docker.internal
    assert "--disable=traefik@server:0" in create
    assert "-p 127.0.0.1:8080:8080@server:0:direct" in create


def test_ensure_cluster_starts_a_stopped_cluster_and_leaves_a_running_one() -> None:
    runner = FakeRunner({"cluster list": cluster_list(platform=True, **{"cell-1": False})})
    clusters = toolkit(runner)
    clusters.ensure_cluster("platform")
    clusters.ensure_cluster("cell-1")
    commands = runner.commands()
    assert not any("cluster create" in c for c in commands)
    assert any(c.endswith("cluster start cellarch-cell-1") for c in commands)
    assert not any("cluster start cellarch-platform" in c for c in commands)


def test_network_is_created_only_when_missing() -> None:
    present = FakeRunner({"network ls": "cellarch\n"})
    toolkit(present).ensure_network()
    assert not any("network create" in c for c in present.commands())
    missing = FakeRunner({"network ls": "bridge\n"})
    toolkit(missing).ensure_network()
    assert any(c.endswith("network create cellarch") for c in missing.commands())


def test_helm_values_go_in_a_temporary_file_never_on_the_command_line() -> None:
    runner = FakeRunner()
    toolkit(runner).helm_upgrade(
        "cell-1", "cell", Path("charts/cell"), {"secrets": {"internalToken": "top-secret"}}
    )
    ((cmd, _),) = runner.calls
    joined = " ".join(cmd)
    assert "top-secret" not in joined
    assert "--kube-context k3d-cellarch-cell-1" in joined
    assert "upgrade --install cell" in joined and "--wait" in joined
    values_file = cmd[cmd.index("-f") + 1]
    assert json.loads(runner.files[values_file])["secrets"]["internalToken"] == "top-secret"
    assert not Path(values_file).exists()  # deleted afterwards


def test_helm_reuse_values_for_existing_releases() -> None:
    runner = FakeRunner()
    toolkit(runner).helm_upgrade("cell-2", "cell", Path("charts/cell"), {}, reuse_values=True)
    assert "--reuse-values" in runner.commands()[0]


def test_node_ip_on_the_private_network() -> None:
    runner = FakeRunner({"docker inspect": "172.20.0.3\n"})
    assert toolkit(runner).node_ip("cell-1") == "172.20.0.3"
    assert "k3d-cellarch-cell-1-server-0" in runner.commands()[0]


def test_cell_dns_zone_is_applied_to_the_platform_cluster() -> None:
    runner = FakeRunner({"get configmap coredns-custom": ""})
    changed = toolkit(runner).set_cell_dns({"cell-2": "172.20.0.5", "cell-1": "172.20.0.3"})
    assert changed
    apply_cmd, manifest = next((c, s) for c, s in runner.calls if "apply" in c)
    assert "k3d-cellarch-platform" in " ".join(apply_cmd)
    assert manifest is not None
    assert "cellarch.internal:53" in manifest
    assert manifest.index("cell-1.cellarch.internal") < manifest.index("cell-2.cellarch.internal")
    assert any("rollout restart deployment coredns" in c for c in runner.commands())


def test_cell_dns_is_left_alone_when_unchanged() -> None:
    zone = LocalClusters.dns_zone({"cell-1": "172.20.0.3"})
    runner = FakeRunner({"get configmap coredns-custom": zone})
    assert not toolkit(runner).set_cell_dns({"cell-1": "172.20.0.3"})
    assert not any("apply" in c or "rollout" in c for c in runner.commands())


def test_import_images_stop_and_delete() -> None:
    runner = FakeRunner()
    clusters = toolkit(runner)
    clusters.import_images("cell-1", ["cellarch/cell-gateway:dev"])
    clusters.stop("cell-1")
    clusters.delete("cell-1")
    assert runner.commands() == [
        "k3d image import cellarch/cell-gateway:dev -c cellarch-cell-1",
        "k3d cluster stop cellarch-cell-1",
        "k3d cluster delete cellarch-cell-1",
    ]
    clusters.delete_network()
    assert runner.commands()[-1] == "docker network rm cellarch"


def test_failures_raise_with_the_tool_output() -> None:
    with pytest.raises(ClusterError, match="image import went wrong"):
        toolkit(FakeRunner(fail="image import")).import_images("cell-1", ["x"])


def test_missing_tools_are_reported() -> None:
    def missing(cmd: Sequence[str], stdin: str | None = None) -> Result:
        raise FileNotFoundError(cmd[0])

    with pytest.raises(ClusterError, match="k3d not found"):
        LocalClusters(run=missing).clusters()


def test_default_runner_runs_processes(tmp_path: Path) -> None:
    from cellarch.cli.clusters import find_free_port, run_process

    result = run_process(["python", "-c", "import sys; print(sys.stdin.read().upper())"], "hi")
    assert (result.code, result.stdout.strip()) == (0, "HI")
    assert 1024 < find_free_port() < 65536
