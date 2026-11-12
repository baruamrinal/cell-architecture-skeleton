"""Local development tasks behind `.\\make <target>` (Windows) and `make <target>`.

The local platform is Kubernetes: a platform cluster (router, control plane) plus one
k3d cluster per cell, on a private Docker network (docs/plan.md §8.4-8.5).

    up      set up .venv, load .env, start the clusters, build and load the images,
            install the Helm charts, show status
    down    stop the clusters (data is kept)
    clean   delete the clusters and their data
    status  platform status (cellctl status)
    build   build the images only
    test    run the test suite with coverage
    setup   (re)install the project into .venv

Standard library only, so it runs before the virtual environment exists.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Shared with cellctl; standard library only, so importable before .venv exists.
sys.path.insert(0, str(ROOT / "src"))
from cellarch.cli.clusters import PLATFORM, ClusterError, LocalClusters, cell_url  # noqa: E402
from cellarch.common.contracts import is_valid_tenant_id  # noqa: E402
from cellarch.common.dotenv import load_env_file, parse_env  # noqa: E402

__all__ = ["parse_env"]  # re-exported for callers of this module

ENV_FILE = ".env"
DEFAULT_ADMIN_TOKEN = "dev-admin-token"  # noqa: S105 - local development default
INSTALL_MARKER = ".cellarch-installed"
RELEASE = "cellarch"
TAG = "dev"
IMAGES = ["router", "control-plane", "cell-gateway", "user-account", "order-processing"]
PLATFORM_IMAGES = ["router", "control-plane"]
CELL_IMAGES = ["cell-gateway"]
ROUTER_PORT, CONTROL_PLANE_PORT = 8080, 8000

Runner = Callable[[Sequence[str], Mapping[str, str]], int]

HELP = """\
Usage: .\\make <target>   (macOS/Linux: make <target>)

  make up      set up .venv, start the k3d clusters, build the images, install the charts
  make down    stop the clusters (data is kept)
  make clean   delete the clusters and all their data
  make status  show platform status
  make build   build the images only
  make test    run the tests
  make setup   reinstall the project into .venv

Start with a dot on Windows to stay in the activated .venv:  . .\\make up
"""


def image(name: str) -> str:
    return f"cellarch/{name}:{TAG}"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def load_env(root: Path) -> dict[str, str] | None:
    return load_env_file(root / ENV_FILE)


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


def seed_cells(env: Mapping[str, str]) -> list[str]:
    cells = [c.strip() for c in env.get("CELLS", "cell-1").split(",") if c.strip()]
    invalid = [c for c in cells if not is_valid_tenant_id(c)]
    if invalid or not cells:
        raise ValueError(f"CELLS must list cell ids like cell-1, not {invalid or 'nothing'}")
    return cells


def cell_token(env: Mapping[str, str], cell_id: str) -> str:
    """CELL_1_TOKEN for cell-1, and so on."""
    return env.get(f"{cell_id.upper().replace('-', '_')}_TOKEN", f"dev-{cell_id}-token")


def platform_values(env: Mapping[str, str], seeds: Sequence[str]) -> dict[str, Any]:
    control_plane: dict[str, Any] = {
        "seedCells": {cell: cell_url(cell) for cell in seeds},
        "service": {"type": "LoadBalancer", "port": CONTROL_PLANE_PORT},
    }
    limits = {
        tier: env[f"RATE_LIMIT_{tier.upper()}"]
        for tier in ("standard", "premium")
        if env.get(f"RATE_LIMIT_{tier.upper()}")
    }
    if limits:
        control_plane["rateLimits"] = limits
    if env.get("DEFAULT_CELL_CAPACITY"):
        control_plane["defaultCellCapacity"] = int(env["DEFAULT_CELL_CAPACITY"])
    return {
        "image": {"tag": TAG},
        "secrets": {
            "adminToken": env.get("ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN),
            "routerToken": env.get("ROUTER_TOKEN", "dev-router-token"),
            "metricsToken": env.get("ROUTER_METRICS_TOKEN", "dev-metrics-token"),
            "cellCredentials": {cell: cell_token(env, cell) for cell in seeds},
        },
        "controlPlane": control_plane,
        "router": {"service": {"type": "LoadBalancer", "port": ROUTER_PORT}},
        "redis": {"enabled": env.get("RATE_LIMIT_BACKEND") == "redis"},
    }


def build_images(root: Path, run: Runner, env: Mapping[str, str]) -> int:
    for name in IMAGES:
        code = run(["docker", "build", "--target", name, "-t", image(name), str(root)], env)
        if code:
            return code
    return 0


def up(
    root: Path,
    run: Runner,
    clusters: LocalClusters,
    env: Mapping[str, str],
    port_check: Callable[[int], bool],
) -> int:
    seeds = seed_cells(env)
    existing = clusters.clusters()
    if PLATFORM not in existing and any(port_check(p) for p in (ROUTER_PORT, CONTROL_PLANE_PORT)):
        print(
            f"error: ports {ROUTER_PORT}/{CONTROL_PLANE_PORT} are in use by another program, "
            "probably the old Docker Compose stack. Stop it first:\n"
            "  docker compose -f deploy/docker-compose.yml down",
            file=sys.stderr,
        )
        return 2

    print("Starting the platform cluster and the cell clusters...")
    clusters.ensure_network()
    ports = [f"127.0.0.1:{p}:{p}" for p in (ROUTER_PORT, CONTROL_PLANE_PORT)]
    clusters.ensure_cluster(PLATFORM, ports=ports)
    cells = sorted(set(seeds) | set(clusters.cell_ids()))
    for cell in cells:
        clusters.ensure_cluster(cell)

    print("Building the images...")
    code = build_images(root, run, env)
    if code:
        return code

    print("Loading images and installing the charts...")
    clusters.import_images(PLATFORM, [image(i) for i in PLATFORM_IMAGES])
    for cell in cells:
        clusters.import_images(cell, [image(i) for i in CELL_IMAGES])
        if cell in seeds:
            values = {"cellId": cell, "secrets": {"internalToken": cell_token(env, cell)}}
            clusters.helm_upgrade(cell, RELEASE, root / "charts" / "cell", values)
        else:  # deployed with cellctl: keep its generated credential
            clusters.helm_upgrade(cell, RELEASE, root / "charts" / "cell", {}, reuse_values=True)
    clusters.set_cell_dns({cell: clusters.node_ip(cell) for cell in cells})
    clusters.helm_upgrade(
        PLATFORM, RELEASE, root / "charts" / "platform", platform_values(env, seeds)
    )
    print(
        f"\nPlatform is up: router http://localhost:{ROUTER_PORT}, "
        f"control plane http://localhost:{CONTROL_PLANE_PORT}\n"
    )
    return run([str(venv_python(root)), "-m", "cellarch.cli.cellctl", "status"], env)


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    run: Runner | None = None,
    clusters: LocalClusters | None = None,
    port_in_use: Callable[[int], bool] = port_in_use,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = args[0] if args else "help"
    run = run or _process_runner(root)
    k3d = clusters or LocalClusters()

    if target == "help":
        print(HELP)
        return 0
    if target not in ("up", "down", "clean", "status", "build", "test", "setup", "token"):
        print(f"error: unknown target {target!r}\n\n{HELP}", file=sys.stderr)
        return 2

    try:
        env = _settings(root, quiet=target == "token")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if target == "token":
        # Internal: the admin token `make up` uses, so make.ps1 can set CELLCTL_TOKEN
        # in the caller's terminal. Not listed in the help.
        print(env["CELLCTL_TOKEN"])
        return 0
    if target == "build":
        return build_images(root, run, env)
    if target == "setup":
        return ensure_venv(root, run, force=True)

    try:
        if target == "down":
            for name, state in sorted(k3d.clusters().items()):
                if state == "running":
                    k3d.stop(name)
            print("Clusters stopped; data is kept. `make up` starts them again.")
            return 0
        if target == "clean":
            for name in sorted(k3d.clusters()):
                k3d.delete(name)
            try:
                k3d.delete_network()
            except ClusterError:
                pass  # no network: nothing to remove
            print("Clusters and their data deleted.")
            return 0

        code = ensure_venv(root, run)
        if code:
            return code
        if target == "test":
            return run([str(venv_python(root)), "-m", "pytest", "--cov"], env)
        if target == "up":
            return up(root, run, k3d, env, port_in_use)
        return run([str(venv_python(root)), "-m", "cellarch.cli.cellctl", "status"], env)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ClusterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


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
