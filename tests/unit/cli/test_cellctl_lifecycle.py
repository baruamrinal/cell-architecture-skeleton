from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.cli.cellctl import main
from cellarch.cli.docker import DockerError, LocalDocker
from cellarch.control_plane.app import ControlPlaneSettings, create_app

from ..control_plane.test_cell_lifecycle_unit import fake_cells

ADMIN = "admin-t"
CELL_3_TOKEN = "cell-3-credential-0123"


@pytest.fixture
def control_plane(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"),
            admin_token=ADMIN,
            router_token="router-t",
            cell_credentials={"cell-1": "c1"},
        ),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(fake_cells({"cell-1": "c1", "cell-3": CELL_3_TOKEN}))
        ),
    )
    with TestClient(app, base_url="http://cp") as client:
        yield client


class RecordingDocker:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def compose(self, files: Sequence[Path], *args: str) -> None:
        self.calls.append(("compose", *args))
        if args[0] == self.fail_on:
            raise DockerError(f"{args[0]} failed")

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


def run(client: TestClient, *args: str, docker: Any = None, compose: Path | None = None) -> int:
    prefix = ["--compose-file", str(compose)] if compose else []
    return main([*prefix, *args], client=client, token=ADMIN, docker=docker)


class TestRegisterWithCredential:
    def test_register_passes_the_credential_and_never_prints_it(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run(
            control_plane,
            "cells",
            "register",
            "cell-3",
            "http://cell-3",
            "--credential",
            CELL_3_TOKEN,
        )
        assert code == 0
        out = capsys.readouterr().out
        assert json.loads(out)["cell_id"] == "cell-3"
        assert CELL_3_TOKEN not in out


class TestSetCapacity:
    def test_set_capacity(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        capsys.readouterr()
        assert run(control_plane, "cells", "set-capacity", "cell-1", "12") == 0
        assert json.loads(capsys.readouterr().out)["capacity"] == 12

    def test_capacity_must_be_positive(self, control_plane: TestClient) -> None:
        with pytest.raises(SystemExit) as exc:
            run(control_plane, "cells", "set-capacity", "cell-1", "0")
        assert exc.value.code == 2


class TestStatus:
    def test_status_shows_totals_cells_and_warnings(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1", "--capacity", "1")
        capsys.readouterr()
        assert run(control_plane, "status") == 0
        out = capsys.readouterr().out
        assert "Cells: 1 (1 placeable)" in out
        assert "Free slots: 1" in out
        assert "Target utilization: 75%" in out
        assert "cell-1" in out
        assert "capacity_low" in out

    def test_status_without_warnings(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1", "--capacity", "10")
        capsys.readouterr()
        run(control_plane, "status")
        assert "No warnings." in capsys.readouterr().out


class TestDeploy:
    def test_refuses_an_existing_cell_without_touching_docker(
        self, control_plane: TestClient, compose_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        docker = RecordingDocker()
        assert (
            run(control_plane, "cells", "deploy", "cell-1", docker=docker, compose=compose_file)
            == 1
        )
        assert docker.calls == []
        assert "cell_exists" in capsys.readouterr().err

    def test_invalid_cell_id_is_a_usage_error(
        self, control_plane: TestClient, compose_file: Path
    ) -> None:
        docker = RecordingDocker()
        assert (
            run(control_plane, "cells", "deploy", "Cell_4", docker=docker, compose=compose_file)
            == 2
        )
        assert docker.calls == []

    def test_missing_compose_file_is_a_usage_error(
        self, control_plane: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "nope.yml"
        assert run(control_plane, "cells", "deploy", "cell-4", compose=missing) == 2
        assert "compose file not found" in capsys.readouterr().err

    def test_container_failure_cleans_up(
        self, control_plane: TestClient, compose_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        docker = RecordingDocker(fail_on="up")
        assert (
            run(control_plane, "cells", "deploy", "cell-4", docker=docker, compose=compose_file)
            == 1
        )
        assert ("compose", "rm", "--stop", "--force", "cell-4") in docker.calls
        assert ("volume-rm", "cellarch_cell-4-data") in docker.calls
        assert not (compose_file.parent / "cells" / "cell-4.yml").exists()
        assert "up failed" in capsys.readouterr().err

    def test_failure_keeps_a_data_volume_that_already_existed(
        self, control_plane: TestClient, compose_file: Path
    ) -> None:
        class ExistingVolume(RecordingDocker):
            def volume_exists(self, name: str) -> bool:
                return True

        docker = ExistingVolume(fail_on="up")
        assert (
            run(control_plane, "cells", "deploy", "cell-4", docker=docker, compose=compose_file)
            == 1
        )
        assert not any(call[0] == "volume-rm" for call in docker.calls)

    def test_generated_compose_file(self, control_plane: TestClient, compose_file: Path) -> None:
        # The fake cells don't know cell-4's random token, so registration fails afterwards;
        # inspect the file through the up call instead.
        captured: dict[str, Any] = {}

        class Capturing(RecordingDocker):
            def compose(self, files: Sequence[Path], *args: str) -> None:
                if args[0] == "up":
                    captured.update(json.loads(files[-1].read_text(encoding="utf-8")))
                    captured["files"] = [str(f) for f in files]
                super().compose(files, *args)

        run(
            control_plane,
            "cells",
            "deploy",
            "cell-4",
            "--image",
            "registry/cellarch:1.2",
            docker=Capturing(),
            compose=compose_file,
        )
        service = captured["services"]["cell-4"]
        assert service["image"] == "registry/cellarch:1.2"
        assert service["environment"]["CELL_ID"] == "cell-4"
        assert service["volumes"] == ["cell-4-data:/data"]
        assert service["read_only"] is True
        assert service["networks"] == ["platform"]
        assert "cell-4-data" in captured["volumes"]
        assert captured["files"][0] == str(compose_file)


class TestDecommission:
    def test_refused_while_tenants_remain(
        self, control_plane: TestClient, compose_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        run(control_plane, "tenants", "create", "acme")
        docker = RecordingDocker()
        assert (
            run(
                control_plane,
                "cells",
                "decommission",
                "cell-1",
                docker=docker,
                compose=compose_file,
            )
            == 1
        )
        assert docker.calls == []
        assert "cell_not_empty" in capsys.readouterr().err

    def test_cell_not_deployed_by_cellctl_is_only_deregistered(
        self, control_plane: TestClient, compose_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        docker = RecordingDocker()
        code = run(
            control_plane,
            "cells",
            "decommission",
            "cell-1",
            "--delete-data",
            docker=docker,
            compose=compose_file,
        )
        assert code == 0
        assert docker.calls == []
        assert "not deployed by cellctl" in capsys.readouterr().err

    def test_container_removal_failure_is_reported(
        self, control_plane: TestClient, compose_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        cells_dir = compose_file.parent / "cells"
        cells_dir.mkdir()
        (cells_dir / "cell-1.yml").write_text("{}", encoding="utf-8")
        docker = RecordingDocker(fail_on="rm")
        assert (
            run(
                control_plane,
                "cells",
                "decommission",
                "cell-1",
                docker=docker,
                compose=compose_file,
            )
            == 1
        )
        assert "rm failed" in capsys.readouterr().err

    def test_keeps_data_by_default(self, control_plane: TestClient, compose_file: Path) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        cells_dir = compose_file.parent / "cells"
        cells_dir.mkdir()
        (cells_dir / "cell-1.yml").write_text("{}", encoding="utf-8")
        docker = RecordingDocker()
        assert (
            run(
                control_plane,
                "cells",
                "decommission",
                "cell-1",
                docker=docker,
                compose=compose_file,
            )
            == 0
        )
        assert docker.calls == [("compose", "rm", "--stop", "--force", "cell-1")]


class TestLocalDocker:
    def test_builds_compose_commands(self) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        docker = LocalDocker(run=fake_run)
        docker.compose([Path("a.yml"), Path("b.yml")], "up", "-d", "cell-4")
        docker.remove_volume("cellarch_cell-4-data")
        assert docker.volume_exists("v")
        assert seen == [
            ["docker", "compose", "-f", "a.yml", "-f", "b.yml", "up", "-d", "cell-4"],
            ["docker", "volume", "rm", "cellarch_cell-4-data"],
            ["docker", "volume", "inspect", "v"],
        ]

    def test_volume_exists_is_false_when_inspect_fails(self) -> None:
        def failing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, "", "no such volume")

        assert not LocalDocker(run=failing).volume_exists("v")

    def test_failures_raise_with_docker_output(self) -> None:
        def failing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, "", "container unhealthy\n")

        with pytest.raises(DockerError, match="container unhealthy"):
            LocalDocker(run=failing).compose([Path("a.yml")], "up")

    def test_missing_docker_cli(self) -> None:
        def missing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("docker")

        with pytest.raises(DockerError, match="docker CLI not found"):
            LocalDocker(run=missing).remove_volume("v")


class TestEdgeCases:
    def test_deploy_reports_control_plane_errors_before_touching_docker(
        self, compose_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthenticated", "message": "bad token"})

        client = httpx.Client(transport=httpx.MockTransport(unauthorized), base_url="http://cp")
        docker = RecordingDocker()
        code = main(
            ["--compose-file", str(compose_file), "cells", "deploy", "cell-4"],
            client=client,
            token="wrong",
            docker=docker,
        )
        assert code == 1
        assert docker.calls == []
        assert "unauthenticated" in capsys.readouterr().err

    def test_deploy_warns_when_cleanup_also_fails(
        self, control_plane: TestClient, compose_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class BrokenDocker(RecordingDocker):
            def compose(self, files: Sequence[Path], *args: str) -> None:
                self.calls.append(("compose", *args))
                raise DockerError(f"{args[0]} failed")

        assert (
            run(
                control_plane,
                "cells",
                "deploy",
                "cell-4",
                docker=BrokenDocker(),
                compose=compose_file,
            )
            == 1
        )
        err = capsys.readouterr().err
        assert "cleanup incomplete" in err
        assert not (compose_file.parent / "cells" / "cell-4.yml").exists()

    def test_status_with_no_cells(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(control_plane, "status") == 0
        out = capsys.readouterr().out
        assert "Cells: 0 (0 placeable)" in out
        assert "no_capacity" in out
