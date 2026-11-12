"""Tests for scripts/dev.py, the logic behind `make up` / `.\\make up`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import dev


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


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "docker-compose.yml").write_text("name: cellarch\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    return tmp_path


def make_venv(root: Path) -> Path:
    python = dev.venv_python(root)
    python.parent.mkdir(parents=True)
    python.write_text("")
    return python


class TestParseEnv:
    def test_parses_values_comments_quotes_and_export(self) -> None:
        text = (
            "# comment\n"
            "\n"
            "ADMIN_TOKEN=abc\n"
            "export ROUTER_TOKEN=def\n"
            'QUOTED="with spaces"\n'
            "SINGLE='x=y'\n"
            "EMPTY=\n"
        )
        assert dev.parse_env(text) == {
            "ADMIN_TOKEN": "abc",
            "ROUTER_TOKEN": "def",
            "QUOTED": "with spaces",
            "SINGLE": "x=y",
            "EMPTY": "",
        }

    @pytest.mark.parametrize("line", ["no-equals-sign", "=value", "BAD KEY=x"])
    def test_malformed_lines_name_the_line(self, line: str) -> None:
        with pytest.raises(ValueError, match="line 2"):
            dev.parse_env(f"OK=1\n{line}\n")


class TestComposeFiles:
    def test_base_then_extras_then_deployed_cells(self, root: Path) -> None:
        (root / "deploy" / "cell-3.override.yml").write_text("{}")
        cells = root / "deploy" / "cells"
        cells.mkdir()
        (cells / "cell-5.yml").write_text("{}")
        (cells / "cell-4.yml").write_text("{}")
        (cells / "notes.txt").write_text("ignored")

        files = dev.compose_files(root, {"EXTRA_COMPOSE_FILES": " deploy/cell-3.override.yml , "})

        assert [f.relative_to(root).as_posix() for f in files] == [
            "deploy/docker-compose.yml",
            "deploy/cell-3.override.yml",
            "deploy/cells/cell-4.yml",
            "deploy/cells/cell-5.yml",
        ]

    def test_missing_extra_file_is_an_error(self, root: Path) -> None:
        with pytest.raises(dev.DevError, match="deploy/nope.yml"):
            dev.compose_files(root, {"EXTRA_COMPOSE_FILES": "deploy/nope.yml"})

    def test_compose_command_uses_env_file_only_when_present(self, root: Path) -> None:
        files = dev.compose_files(root, {})
        assert dev.compose_command(root, files, "ps")[:3] == ["docker", "compose", "-f"]
        (root / ".env").write_text("A=1\n")
        assert dev.compose_command(root, files, "ps")[:4] == [
            "docker",
            "compose",
            "--env-file",
            str(root / ".env"),
        ]


class TestVenv:
    def test_creates_venv_and_installs_when_missing(self, root: Path) -> None:
        run = FakeRunner()

        def create_venv(cmd: Sequence[str], env: Mapping[str, str]) -> int:
            if "venv" in cmd:
                make_venv(root)
            return run(cmd, env)

        assert dev.ensure_venv(root, create_venv, python="system-python") == 0
        python = str(dev.venv_python(root))
        assert run.commands == [
            ["system-python", "-m", "venv", str(root / ".venv")],
            [python, "-m", "pip", "install", "--quiet", "-e", ".[dev]"],
        ]

    def test_stops_when_the_venv_cannot_be_created(self, root: Path) -> None:
        run = FakeRunner(fail_on="venv")
        assert dev.ensure_venv(root, run, python="system-python") == 1
        assert len(run.commands) == 1  # no install attempted

    def test_skips_install_when_up_to_date(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner()
        dev.ensure_venv(root, run)
        dev.ensure_venv(root, run)
        assert len(run.commands) == 1  # installed once, then the marker matches

    def test_reinstalls_when_pyproject_changes(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner()
        dev.ensure_venv(root, run)
        (root / "pyproject.toml").write_text("[project]\nname = 'changed'\n")
        dev.ensure_venv(root, run)
        assert len(run.commands) == 2

    def test_force_reinstalls(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner()
        dev.ensure_venv(root, run)
        dev.ensure_venv(root, run, force=True)
        assert len(run.commands) == 2

    def test_failed_install_is_retried_next_time(self, root: Path) -> None:
        make_venv(root)
        assert dev.ensure_venv(root, FakeRunner(fail_on="pip")) == 1
        run = FakeRunner()
        dev.ensure_venv(root, run)
        assert len(run.commands) == 1

    def test_venv_python_path_per_platform(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("dev.os.name", "nt")
        assert dev.venv_python(root) == root / ".venv" / "Scripts" / "python.exe"
        monkeypatch.setattr("dev.os.name", "posix")
        assert dev.venv_python(root) == root / ".venv" / "bin" / "python"


class TestTargets:
    def test_up_installs_builds_starts_and_shows_status(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_venv(root)
        (root / ".env").write_text("ADMIN_TOKEN=secret-admin\n")
        run = FakeRunner()

        assert dev.main(["up"], root=root, run=run) == 0

        commands = run.commands
        assert commands[0][-4:] == ["install", "--quiet", "-e", ".[dev]"]
        up = commands[1]
        assert up[:4] == ["docker", "compose", "--env-file", str(root / ".env")]
        assert up[-6:] == ["up", "--build", "--detach", "--wait", "--wait-timeout", "180"]
        assert commands[2][1:] == ["-m", "cellarch.cli.cellctl", "status"]
        status_env = run.calls[2][1]
        assert status_env["CELLCTL_TOKEN"] == "secret-admin"
        assert status_env["ADMIN_TOKEN"] == "secret-admin"
        assert "Using settings from .env" in capsys.readouterr().out

    def test_up_without_env_file_uses_defaults(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_venv(root)
        run = FakeRunner()
        assert dev.main(["up"], root=root, run=run) == 0
        assert run.calls[-1][1]["CELLCTL_TOKEN"] == "dev-admin-token"
        assert "No .env file" in capsys.readouterr().out

    def test_explicit_cellctl_token_wins(self, root: Path) -> None:
        make_venv(root)
        (root / ".env").write_text("ADMIN_TOKEN=a\nCELLCTL_TOKEN=b\n")
        run = FakeRunner()
        dev.main(["status"], root=root, run=run)
        assert run.calls[-1][1]["CELLCTL_TOKEN"] == "b"

    def test_up_stops_when_docker_fails(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner(fail_on="up")
        assert dev.main(["up"], root=root, run=run) == 1
        assert not any("cellarch.cli.cellctl" in cmd for cmd in run.commands)

    def test_up_stops_when_install_fails(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner(fail_on="pip")
        assert dev.main(["up"], root=root, run=run) == 1
        assert len(run.commands) == 1

    @pytest.mark.parametrize(
        ("target", "tail"),
        [("down", ["down"]), ("build", ["build"])],
    )
    def test_docker_targets(self, root: Path, target: str, tail: list[str]) -> None:
        run = FakeRunner()
        assert dev.main([target], root=root, run=run) == 0
        assert run.commands == [run.commands[0]]
        assert run.commands[0][:2] == ["docker", "compose"]
        assert run.commands[0][-len(tail) :] == tail

    def test_test_target_runs_pytest_with_coverage(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner()
        assert dev.main(["test"], root=root, run=run) == 0
        assert run.commands[-1][1:] == ["-m", "pytest", "--cov"]

    def test_setup_forces_a_reinstall(self, root: Path) -> None:
        make_venv(root)
        run = FakeRunner()
        dev.main(["setup"], root=root, run=run)
        dev.main(["setup"], root=root, run=run)
        assert len(run.commands) == 2

    def test_bad_env_file_is_reported(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (root / ".env").write_text("not valid\n")
        assert dev.main(["down"], root=root, run=FakeRunner()) == 2
        assert ".env line 1" in capsys.readouterr().err

    def test_help_and_unknown_targets(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert dev.main(["help"], root=root, run=FakeRunner()) == 0
        assert "make up" in capsys.readouterr().out
        assert dev.main([], root=root, run=FakeRunner()) == 0
        assert dev.main(["deploy-everything"], root=root, run=FakeRunner()) == 2
        assert "unknown target" in capsys.readouterr().err
