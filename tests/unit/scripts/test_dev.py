"""Tests for scripts/dev.py, the logic behind `make up` / `.\\make up` (k3d runtime)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import dev
from cellarch.cli.clusters import ClusterError


class FakeRunner:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, cmd: Sequence[str], env: Mapping[str, str]) -> int:
        self.calls.append((list(cmd), dict(env)))
        return 1 if self.fail_on and self.fail_on in cmd else 0

    @property
    def commands(self) -> list[list[str]]:
        return [cmd for cmd, _ in self.calls]


class FakeClusters:
    """Stands in for LocalClusters: records what `make` asks of k3d/Helm/kubectl."""

    def __init__(self, existing: dict[str, str] | None = None, fail_on: str | None = None):
        self.existing = dict(existing or {})
        self.fail_on = fail_on
        self.calls: list[tuple[Any, ...]] = []

    def _record(self, *call: Any) -> None:
        self.calls.append(call)
        if self.fail_on == call[0]:
            raise ClusterError(f"{call[0]} failed")

    def clusters(self) -> dict[str, str]:
        return dict(self.existing)

    def cell_ids(self) -> list[str]:
        return sorted(name for name in self.existing if name != "platform")

    def ensure_network(self) -> None:
        self._record("network")

    def ensure_cluster(self, short: str, *, ports: Iterable[str] = ()) -> None:
        self._record("cluster", short, tuple(ports))
        self.existing[short] = "running"

    def import_images(self, short: str, images: Sequence[str]) -> None:
        self._record("import", short, tuple(images))

    def helm_upgrade(
        self,
        short: str,
        release: str,
        chart: Path,
        values: Mapping[str, Any],
        *,
        reuse_values: bool = False,
    ) -> None:
        self._record("helm", short, release, chart.name, dict(values), reuse_values)

    def helm_uninstall(self, short: str, release: str) -> None:
        self._record("uninstall", short, release)

    def node_ip(self, short: str) -> str:
        return f"172.20.0.{len(short)}"

    def set_cell_dns(self, entries: Mapping[str, str]) -> bool:
        self._record("dns", dict(entries))
        return True

    def stop(self, short: str) -> None:
        self._record("stop", short)

    def delete(self, short: str) -> None:
        self._record("delete", short)

    def delete_network(self) -> None:
        self._record("delete-network")

    def helm(self, short: str, release: str = "cellarch") -> tuple[Any, ...]:
        return next(c for c in self.calls if c[0] == "helm" and c[1:3] == (short, release))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "charts").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    mgr = tmp_path / "src" / "cellarch" / "mgr"
    mgr.mkdir(parents=True)
    (mgr / "__init__.py").write_text('"""mgr."""\n\n__version__ = "0.4.2"\n')
    return tmp_path


def clean_git(cmd: Sequence[str]) -> str:
    """git as a clean checkout at commit abc1234 answers it."""
    return "abc1234\n" if "rev-parse" in cmd else ""


MGR_TAG = "0.4.2-abc1234"


def make_venv(root: Path) -> Path:
    python = dev.venv_python(root)
    python.parent.mkdir(parents=True)
    python.write_text("")
    (root / ".venv" / dev.INSTALL_MARKER).write_text(
        __import__("hashlib").sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    )
    return python


def up(root: Path, clusters: FakeClusters, run: FakeRunner | None = None, **kw: Any) -> int:
    return dev.main(
        [kw.get("target", "up")],
        root=root,
        run=run or FakeRunner(),
        clusters=clusters,
        port_in_use=kw.get("port_in_use", lambda port: False),
        answers=kw.get("answers", lambda url: True),
        sleep=kw.get("sleep", lambda seconds: None),
        git=kw.get("git", clean_git),
        now=kw.get("now", lambda: 1_700_000_000.0),
    )


class TestWaitsForThePlatform:
    """helm --wait doesn't cover k3s's load balancer pods: wait until the API answers."""

    def test_status_runs_once_the_control_plane_answers(self, root: Path) -> None:
        make_venv(root)
        replies = iter([False, False, True])
        probed: list[str] = []

        def answers(url: str) -> bool:
            probed.append(url)
            return next(replies)

        run = FakeRunner()
        assert up(root, FakeClusters(), run, answers=answers) == 0
        assert probed == ["http://127.0.0.1:8000/healthz"] * 3
        assert "status" in " ".join(run.calls[-1][0])

    def test_gives_up_with_a_clear_error(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_venv(root)
        waited: list[float] = []
        assert up(root, FakeClusters(), answers=lambda url: False, sleep=waited.append) == 1
        assert sum(waited) >= 120
        assert "control plane did not answer" in capsys.readouterr().err


def test_answers_probes_a_real_http_server() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Ok(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/healthz" else 503)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert dev.answers(f"{base}/healthz")
        assert not dev.answers(f"{base}/other")
    finally:
        server.shutdown()
    assert not dev.answers("http://127.0.0.1:1/healthz")


class TestParseEnv:
    def test_parses_values_comments_quotes_and_export(self) -> None:
        text = '# c\n\nADMIN_TOKEN=abc\nexport ROUTER_TOKEN=def\nQUOTED="a b"\nEMPTY=\n'
        assert dev.parse_env(text) == {
            "ADMIN_TOKEN": "abc",
            "ROUTER_TOKEN": "def",
            "QUOTED": "a b",
            "EMPTY": "",
        }

    @pytest.mark.parametrize("line", ["no-equals-sign", "=value", "BAD KEY=x"])
    def test_malformed_lines_name_the_line(self, line: str) -> None:
        with pytest.raises(ValueError, match="line 2"):
            dev.parse_env(f"OK=1\n{line}\n")


class TestVenv:
    def test_creates_venv_and_installs_when_missing(self, root: Path) -> None:
        run = FakeRunner()

        def create_venv(cmd: Sequence[str], env: Mapping[str, str]) -> int:
            if "venv" in cmd:
                python = dev.venv_python(root)
                python.parent.mkdir(parents=True)
                python.write_text("")
            return run(cmd, env)

        assert dev.ensure_venv(root, create_venv, python="system-python") == 0
        assert run.commands == [
            ["system-python", "-m", "venv", str(root / ".venv")],
            [str(dev.venv_python(root)), "-m", "pip", "install", "--quiet", "-e", ".[dev]"],
        ]

    def test_skips_install_when_up_to_date_and_reinstalls_on_change(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner()
        dev.ensure_venv(root, run)
        assert run.commands == []
        (root / "pyproject.toml").write_text("[project]\nname = 'changed'\n")
        dev.ensure_venv(root, run)
        dev.ensure_venv(root, run, force=True)
        assert len(run.commands) == 2

    def test_failures(self, root: Path) -> None:
        assert dev.ensure_venv(root, FakeRunner(fail_on="venv"), python="p") == 1
        make_venv(root)
        (root / "pyproject.toml").write_text("[project]\nname = 'changed'\n")
        assert dev.ensure_venv(root, FakeRunner(fail_on="pip")) == 1

    def test_venv_python_path_per_platform(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("dev.os.name", "nt")
        assert dev.venv_python(root) == root / ".venv" / "Scripts" / "python.exe"
        monkeypatch.setattr("dev.os.name", "posix")
        assert dev.venv_python(root) == root / ".venv" / "bin" / "python"


class TestUp:
    def test_creates_two_small_clusters_builds_installs_and_shows_status(self, root: Path) -> None:
        make_venv(root)
        (root / ".env").write_text("ADMIN_TOKEN=a-secret\nCELL_1_TOKEN=c1-secret\n")
        clusters, run = FakeClusters(), FakeRunner()

        assert up(root, clusters, run) == 0

        assert clusters.calls[0] == ("network",)
        assert clusters.calls[1] == (
            "cluster",
            "platform",
            ("127.0.0.1:8080:8080", "127.0.0.1:8000:8000"),
        )
        assert clusters.calls[2] == ("cluster", "cell-1", ())
        builds = [c for c in run.commands if c[:2] == ["docker", "build"]]
        assert [c[3] for c in builds] == [*dev.IMAGES, "mgr"]
        assert (
            "import",
            "platform",
            ("cellarch/router:dev", "cellarch/control-plane:dev"),
        ) in clusters.calls
        assert (
            "import",
            "cell-1",
            (
                "cellarch/cell-gateway:dev",
                "cellarch/user-account:dev",
                "cellarch/order-processing:dev",
            ),
        ) in clusters.calls

        cell = clusters.helm("cell-1")
        assert cell[2:4] == ("cellarch", "cell")
        assert cell[4] == {"cellId": "cell-1", "secrets": {"internalToken": "c1-secret"}}
        assert ("dns", {"cell-1": "172.20.0.6"}) in clusters.calls

        platform = clusters.helm("platform")[4]
        assert platform["secrets"]["adminToken"] == "a-secret"
        assert platform["secrets"]["cellCredentials"] == {"cell-1": "c1-secret"}
        assert platform["controlPlane"]["seedCells"] == {
            "cell-1": "http://cell-1.cellarch.internal:8000"
        }
        assert platform["router"]["service"] == {"type": "ClusterIP", "port": 8000}
        assert platform["edge"] == {
            "enabled": True,
            "service": {"type": "LoadBalancer", "port": 8080},
        }
        assert platform["sessions"]["baseDomain"] == "localhost"
        assert platform["controlPlane"]["service"] == {"type": "LoadBalancer", "port": 8000}
        assert platform["redis"] == {"enabled": False}
        # The platform is installed last, once every cell answers by name.
        assert clusters.calls[-1][0:2] == ("helm", "platform")
        assert run.commands[-1][-1] == "status"

    def test_installs_mgr_in_every_cell_and_the_dev_identity_provider(self, root: Path) -> None:
        make_venv(root)
        (root / ".env").write_text("CELLS=cell-1, cell-2\n")
        clusters, run = FakeClusters(), FakeRunner()

        assert up(root, clusters, run) == 0

        build = ["docker", "build", "--target", "mgr", "-t", f"cellarch/mgr:{MGR_TAG}", str(root)]
        assert build in run.commands
        for cell in ("cell-1", "cell-2"):
            assert ("import", cell, (f"cellarch/mgr:{MGR_TAG}",)) in clusters.calls
            mgr = next(c for c in clusters.calls if c[:3] == ("helm", cell, "mgr"))
            assert mgr[3:] == ("mgr", {"cellId": cell, "image": {"tag": MGR_TAG}}, False)
        assert ("import", "platform", ("cellarch/dev-idp:dev",)) in clusters.calls
        idp = next(c for c in clusters.calls if c[:3] == ("helm", "platform", "dev-idp"))
        assert idp[3] == "dev-idp"
        assert idp[4]["baseDomain"] == "localhost"
        assert dev.read_versions(root) == {"mgr": MGR_TAG}

    def test_mgr_from_a_changed_working_tree_gets_a_fresh_tag(self, root: Path) -> None:
        make_venv(root)

        def dirty(cmd: Sequence[str]) -> str:
            return " M src/cellarch/mgr/app.py\n" if "status" in cmd else clean_git(cmd)

        assert up(root, FakeClusters(), git=dirty) == 0
        assert dev.read_versions(root)["mgr"] == "0.4.2-abc1234-dirty.1700000000"

    def test_without_git_the_tag_says_so(self, root: Path) -> None:
        make_venv(root)

        def no_git(cmd: Sequence[str]) -> str:
            raise OSError("git not found")

        assert up(root, FakeClusters(), git=no_git) == 0
        assert dev.read_versions(root)["mgr"] == "0.4.2-local.1700000000"

    def test_more_seed_cells_and_defaults(self, root: Path) -> None:
        make_venv(root)
        (root / ".env").write_text("CELLS=cell-1, cell-2\n")
        clusters = FakeClusters()
        assert up(root, clusters) == 0
        assert clusters.helm("cell-2")[4]["secrets"]["internalToken"] == "dev-cell-2-token"
        assert set(clusters.helm("platform")[4]["controlPlane"]["seedCells"]) == {
            "cell-1",
            "cell-2",
        }

    def test_cells_added_with_cellctl_are_updated_and_keep_their_values(self, root: Path) -> None:
        make_venv(root)
        clusters = FakeClusters({"platform": "running", "cell-1": "running", "cell-7": "stopped"})
        assert up(root, clusters) == 0
        assert ("cluster", "cell-7", ()) in clusters.calls  # started again
        assert clusters.helm("cell-7")[4:] == ({}, True)  # --reuse-values keeps its credential
        dns = next(c[1] for c in clusters.calls if c[0] == "dns")
        assert set(dns) == {"cell-1", "cell-7"}
        assert "cell-7" not in clusters.helm("platform")[4]["controlPlane"]["seedCells"]

    def test_tier_limits_and_capacity_from_env(self, root: Path) -> None:
        make_venv(root)
        (root / ".env").write_text(
            "RATE_LIMIT_STANDARD=5:10\nDEFAULT_CELL_CAPACITY=8\nROUTER_REPLICAS=3\n"
            "MAX_CONCURRENT_REQUESTS=40\n"
        )
        clusters = FakeClusters()
        up(root, clusters)
        platform = clusters.helm("platform")[4]
        assert platform["controlPlane"]["rateLimits"] == {"standard": "5:10"}
        assert platform["controlPlane"]["defaultCellCapacity"] == 8
        assert "defaultTenantIsolation" not in platform["controlPlane"]
        assert platform["router"]["replicas"] == 3
        assert clusters.helm("cell-1")[4]["gateway"] == {"maxConcurrentRequests": 40}

    def test_redis_backend(self, root: Path) -> None:

        make_venv(root)
        (root / ".env").write_text("RATE_LIMIT_BACKEND=redis\n")
        clusters = FakeClusters()
        up(root, clusters)
        assert clusters.helm("platform")[4]["redis"] == {"enabled": True}

    def test_refuses_when_the_ports_are_taken_by_something_else(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_venv(root)
        clusters = FakeClusters()
        assert up(root, clusters, port_in_use=lambda port: port == 8080) == 2
        assert "docker compose" in capsys.readouterr().err
        assert clusters.calls == []

    def test_ports_in_use_by_our_own_platform_cluster_are_fine(self, root: Path) -> None:
        make_venv(root)
        clusters = FakeClusters({"platform": "running"})
        assert up(root, clusters, port_in_use=lambda port: True) == 0

    @pytest.mark.parametrize("step", ["network", "cluster", "import", "helm", "dns"])
    def test_stops_at_the_first_cluster_failure(
        self, root: Path, step: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_venv(root)
        clusters = FakeClusters(fail_on=step)
        assert up(root, clusters) == 1
        assert f"{step} failed" in capsys.readouterr().err

    def test_stops_when_an_image_does_not_build(self, root: Path) -> None:
        make_venv(root)
        clusters = FakeClusters()
        assert up(root, clusters, FakeRunner(fail_on="build")) == 1
        assert not any(c[0] == "import" for c in clusters.calls)

    def test_stops_when_install_fails(self, root: Path) -> None:
        (root / ".venv").mkdir()
        clusters = FakeClusters()
        assert up(root, clusters, FakeRunner(fail_on="pip")) == 1
        assert clusters.calls == []

    def test_rejects_invalid_cell_ids(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        make_venv(root)
        (root / ".env").write_text("CELLS=Cell One\n")
        assert up(root, FakeClusters()) == 2
        assert "CELLS" in capsys.readouterr().err


class TestDeployMgr:
    """`make deploy-mgr`: ship mgr alone, to every cell, without `make up`."""

    def test_builds_and_upgrades_only_mgr_in_every_running_cell(self, root: Path) -> None:
        make_venv(root)
        clusters = FakeClusters({"platform": "running", "cell-1": "running", "cell-2": "running"})
        run = FakeRunner()

        assert up(root, clusters, run, target="deploy-mgr") == 0

        builds = [c for c in run.commands if c[:2] == ["docker", "build"]]
        build = ["docker", "build", "--target", "mgr", "-t", f"cellarch/mgr:{MGR_TAG}", str(root)]
        assert builds == [build]
        assert [c for c in clusters.calls if c[0] == "helm"] == [
            ("helm", cell, "mgr", "mgr", {"cellId": cell, "image": {"tag": MGR_TAG}}, False)
            for cell in ("cell-1", "cell-2")
        ]
        assert dev.read_versions(root) == {"mgr": MGR_TAG}

    def test_stopped_cells_are_skipped_and_failures_stop_it(self, root: Path) -> None:
        make_venv(root)
        clusters = FakeClusters({"platform": "running", "cell-1": "running", "cell-2": "stopped"})
        assert up(root, clusters, target="deploy-mgr") == 0
        assert not any(c[:2] == ("helm", "cell-2") for c in clusters.calls)
        failing = FakeClusters({"platform": "running", "cell-1": "running"})
        assert up(root, failing, FakeRunner(fail_on="mgr"), target="deploy-mgr") == 1
        nothing = FakeClusters({"platform": "running"})
        assert up(root, nothing, target="deploy-mgr") == 1  # no cells to deploy to

    def test_a_package_without_a_version_is_an_error(self, root: Path) -> None:
        from cellarch.cli.versions import source_version

        (root / "x.py").write_text("print('no version')")
        with pytest.raises(ValueError, match="__version__"):
            source_version(root / "x.py")

    def test_versions_file(self, root: Path) -> None:
        assert dev.read_versions(root) == {}
        dev.write_version(root, "mgr", "1.0.0-a")
        dev.write_version(root, "user-account", "2.0.0-b")
        dev.write_version(root, "mgr", "1.0.1-c")
        assert dev.read_versions(root) == {"mgr": "1.0.1-c", "user-account": "2.0.0-b"}


class TestOtherTargets:
    def call(self, target: str, root: Path, clusters: FakeClusters, run: FakeRunner) -> int:
        return dev.main(
            [target], root=root, run=run, clusters=clusters, port_in_use=lambda p: False
        )

    def test_down_stops_running_clusters_and_keeps_data(self, root: Path) -> None:
        clusters = FakeClusters({"platform": "running", "cell-1": "running", "cell-2": "stopped"})
        assert self.call("down", root, clusters, FakeRunner()) == 0
        assert clusters.calls == [("stop", "cell-1"), ("stop", "platform")]

    def test_clean_deletes_clusters_and_the_network(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clusters = FakeClusters({"platform": "running", "cell-1": "stopped"})
        assert self.call("clean", root, clusters, FakeRunner()) == 0
        assert clusters.calls == [("delete", "cell-1"), ("delete", "platform"), ("delete-network",)]
        assert "deleted" in capsys.readouterr().out

    def test_clean_with_nothing_to_delete(self, root: Path) -> None:
        clusters = FakeClusters(fail_on="delete-network")
        assert self.call("clean", root, clusters, FakeRunner()) == 0  # no network is fine

    def test_down_reports_failures(self, root: Path) -> None:
        clusters = FakeClusters({"platform": "running"}, fail_on="stop")
        assert self.call("down", root, clusters, FakeRunner()) == 1

    def test_build_only_builds(self, root: Path) -> None:
        clusters, run = FakeClusters(), FakeRunner()
        assert self.call("build", root, clusters, run) == 0
        assert [c[3] for c in run.commands] == [*dev.IMAGES, "mgr"]
        assert clusters.calls == []

    def test_status_test_and_setup(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner()
        assert self.call("status", root, FakeClusters(), run) == 0
        assert run.commands[-1][1:] == ["-m", "cellarch.cli.cellctl", "status"]
        assert self.call("test", root, FakeClusters(), run) == 0
        assert run.commands[-1][1:] == ["-m", "pytest", "--cov"]
        assert self.call("setup", root, FakeClusters(), run) == 0
        assert run.commands[-1][-1] == ".[dev]"

    @pytest.mark.parametrize(
        ("env_file", "token"),
        [
            (None, "dev-admin-token"),
            ("ADMIN_TOKEN=x\n", "x"),
            ("ADMIN_TOKEN=a\nCELLCTL_TOKEN=y\n", "y"),
        ],
    )
    def test_token_target(
        self, root: Path, capsys: pytest.CaptureFixture[str], env_file: str | None, token: str
    ) -> None:
        if env_file:
            (root / ".env").write_text(env_file)
        assert self.call("token", root, FakeClusters(), FakeRunner()) == 0
        assert capsys.readouterr().out == f"{token}\n"

    def test_bad_env_file(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (root / ".env").write_text("broken\n")
        assert self.call("token", root, FakeClusters(), FakeRunner()) == 2
        assert self.call("down", root, FakeClusters(), FakeRunner()) == 2
        assert ".env line 1" in capsys.readouterr().err

    def test_help_and_unknown_targets(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert self.call("help", root, FakeClusters(), FakeRunner()) == 0
        out = capsys.readouterr().out
        assert "make up" in out and "make clean" in out
        assert dev.main([], root=root, run=FakeRunner()) == 0
        assert self.call("deploy-everything", root, FakeClusters(), FakeRunner()) == 2
        assert "unknown target" in capsys.readouterr().err


def test_port_in_use_checks_a_real_socket() -> None:
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert dev.port_in_use(server.getsockname()[1])
    assert not dev.port_in_use(1)


def test_runs_with_the_standard_library_only() -> None:
    """make up runs before .venv exists, so dev.py must not need installed packages."""
    import subprocess
    import sys

    scripts = Path(dev.__file__).parent
    code = f"import sys; sys.path.insert(0, {str(scripts)!r}); import dev; print('ok')"
    result = subprocess.run(  # noqa: S603 - fixed command
        [sys.executable, "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
