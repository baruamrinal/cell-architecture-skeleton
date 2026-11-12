"""Local development tasks behind `.\\make <target>` (Windows) and `make <target>`.

    up      set up .venv, load .env, build the image, start every container, show status
    down    stop and remove the containers (data volumes are kept)
    status  platform status (cellctl status)
    build   build the image only
    test    run the test suite with coverage
    setup   (re)install the project into .venv

Standard library only, so it runs before the virtual environment exists.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The .env reader is shared with cellctl. It's standard library only, so it can be
# imported from the source tree before the virtual environment exists.
sys.path.insert(0, str(ROOT / "src"))
from cellarch.common.dotenv import load_env_file, parse_env  # noqa: E402

__all__ = ["parse_env"]  # re-exported for callers of this module

BASE_COMPOSE = Path("deploy") / "docker-compose.yml"
CELLS_DIR = Path("deploy") / "cells"  # compose files written by `cellctl cells deploy`
ENV_FILE = ".env"
DEFAULT_ADMIN_TOKEN = "dev-admin-token"  # noqa: S105 - local development default
INSTALL_MARKER = ".cellarch-installed"

Runner = Callable[[Sequence[str], Mapping[str, str]], int]

HELP = """\
Usage: .\\make <target>   (macOS/Linux: make <target>)

  make up      set up .venv, load .env, build the image, start all containers, show status
  make down    stop and remove the containers (data is kept)
  make status  show platform status
  make build   build the image only
  make test    run the tests
  make setup   reinstall the project into .venv

Start with a dot on Windows to stay in the activated .venv:  . .\\make up
"""


class DevError(Exception):
    """A problem with the local setup, reported without a traceback."""


def load_env(root: Path) -> dict[str, str] | None:
    return load_env_file(root / ENV_FILE)


def compose_files(root: Path, env: Mapping[str, str]) -> list[Path]:
    """Base compose file, then extra files from EXTRA_COMPOSE_FILES, then deployed cells."""
    files = [root / BASE_COMPOSE]
    for name in env.get("EXTRA_COMPOSE_FILES", "").split(","):
        name = name.strip()
        if not name:
            continue
        path = root / name
        if not path.is_file():
            raise DevError(f"EXTRA_COMPOSE_FILES: {name} not found")
        files.append(path)
    files.extend(sorted((root / CELLS_DIR).glob("*.yml")))
    return files


def compose_command(root: Path, files: Sequence[Path], *args: str) -> list[str]:
    cmd = ["docker", "compose"]
    env_file = root / ENV_FILE
    if env_file.is_file():
        cmd += ["--env-file", str(env_file)]
    for path in files:
        cmd += ["-f", str(path)]
    return [*cmd, *args]


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def ensure_venv(
    root: Path, run: Runner, *, python: str = sys.executable, force: bool = False
) -> int:
    """Create .venv if needed and install the project when pyproject.toml changed."""
    venv_py = venv_python(root)
    if not venv_py.exists():
        print("Creating the virtual environment (.venv)...")
        code = run([python, "-m", "venv", str(root / ".venv")], {})
        if code:
            return code

    digest = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    marker = root / ".venv" / INSTALL_MARKER
    if not force and marker.is_file() and marker.read_text(encoding="utf-8") == digest:
        return 0
    print("Installing the project and dev tools into .venv...")
    code = run([str(venv_py), "-m", "pip", "install", "--quiet", "-e", ".[dev]"], {})
    if code == 0:
        marker.write_text(digest, encoding="utf-8")
    return code


def _settings(root: Path, *, quiet: bool = False) -> dict[str, str]:
    env = load_env(root)
    if env is None:
        if not quiet:
            print("No .env file; using development defaults (copy .env.example to .env).")
        env = {}
    elif not quiet:
        print("Using settings from .env.")
    env.setdefault("CELLCTL_TOKEN", env.get("ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN))
    return env


def _status(root: Path, run: Runner, env: Mapping[str, str]) -> int:
    return run([str(venv_python(root)), "-m", "cellarch.cli.cellctl", "status"], env)


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT, run: Runner | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = args[0] if args else "help"
    run = run or _process_runner(root)

    if target == "help":
        print(HELP)
        return 0
    if target == "token":
        # Internal: prints the admin token `make up` uses, so make.ps1 can set
        # CELLCTL_TOKEN in the caller's terminal. Not listed in the help.
        try:
            print(_settings(root, quiet=True)["CELLCTL_TOKEN"])
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if target not in ("up", "down", "status", "build", "test", "setup"):
        print(f"error: unknown target {target!r}\n\n{HELP}", file=sys.stderr)
        return 2

    try:
        env = _settings(root)
        files = compose_files(root, env)
    except (ValueError, DevError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if target == "down":
        return run(compose_command(root, files, "down"), env)
    if target == "build":
        return run(compose_command(root, files, "build"), env)
    if target == "setup":
        return ensure_venv(root, run, force=True)

    code = ensure_venv(root, run)
    if code:
        return code
    if target == "test":
        return run([str(venv_python(root)), "-m", "pytest", "--cov"], env)
    if target == "up":
        up = compose_command(
            root, files, "up", "--build", "--detach", "--wait", "--wait-timeout", "180"
        )
        code = run(up, env)
        if code:
            return code
        print(
            "\nPlatform is up: router http://localhost:8080, control plane http://localhost:8000\n"
        )
    return _status(root, run, env)


def _process_runner(root: Path) -> Runner:  # pragma: no cover - real processes
    def run(cmd: Sequence[str], env: Mapping[str, str]) -> int:
        print(f"> {' '.join(cmd)}", flush=True)
        try:
            # Commands are built by this script, never from user input.
            completed = subprocess.run(list(cmd), cwd=root, env={**os.environ, **env})  # noqa: S603
            return completed.returncode
        except FileNotFoundError:
            print(f"error: {cmd[0]} not found; is it installed and on PATH?", file=sys.stderr)
            return 127

    return run


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
