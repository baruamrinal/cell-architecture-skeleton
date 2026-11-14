"""Local development tasks behind `.\\make <target>` (Windows) and `make <target>`.

The local platform is Kubernetes: a platform cluster (router, control plane) plus one
k3d cluster per cell, on a private Docker network (docs/plan.md §8.4-8.5).

    up      set up .venv, load .env, start the clusters, build and load the images,
            install the Helm charts, show status
    deploy-mgr  build mgr only and upgrade it in every running cell (independent delivery)
    down    stop the clusters (data is kept)
    clean   delete the clusters and their data
    status  platform status (cellctl status)
    build   build the images only
    test    run the test suite with coverage
    setup   (re)install the project into .venv

Standard library only, so it runs before the virtual environment exists.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Shared with cellctl; standard library only, so importable before .venv exists.
sys.path.insert(0, str(ROOT / "src"))
from cellarch.cli.clusters import (  # noqa: E402
    PLATFORM,
    ClusterError,
    ClusterToolkit,
    LocalClusters,
    cell_url,
)
from cellarch.cli.versions import (  # noqa: E402
    Git,
    image_tag,
    read_versions,
    source_version,
    write_version,
)
from cellarch.common.dotenv import load_env_file, parse_env  # noqa: E402
from cellarch.common.ids import is_valid_tenant_id  # noqa: E402

__all__ = ["parse_env", "read_versions", "write_version"]  # re-exported

ENV_FILE = ".env"
DEFAULT_ADMIN_TOKEN = "dev-admin-token"  # noqa: S105 - local development default
INSTALL_MARKER = ".cellarch-installed"
RELEASE = "cellarch"
TAG = "dev"
IMAGES = [
    "router",
    "control-plane",
    "cell-gateway",
    "user-account",
    "order-processing",
    "dev-idp",
]
PLATFORM_IMAGES = ["router", "control-plane"]
DEV_IDP = "dev-idp"  # development-only identity provider (platform cluster)
# mgr is delivered on its own: its own version and tag, its own release in each cell.
MGR, MGR_VERSION_FILE = "mgr", Path("src") / "cellarch" / "mgr" / "__init__.py"
TENANT_BASE_DOMAIN = "localhost"  # tenants' UI hosts: <tenant>.localhost:8080
SESSION_ISSUER = "http://localhost:8080/auth"
# Every cell runs its gateway and its tenants' services.
CELL_IMAGES = ["cell-gateway", "user-account", "order-processing"]
ROUTER_PORT, CONTROL_PLANE_PORT = 8080, 8000
READY_TIMEOUT_SECONDS, READY_POLL_SECONDS = 120, 2

Runner = Callable[[Sequence[str], Mapping[str, str]], int]

HELP = """\
Usage: .\\make <target>   (macOS/Linux: make <target>)

  make up      set up .venv, start the k3d clusters, build the images, install the charts
  make deploy-mgr  build mgr only and upgrade it in every running cell
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


def answers(url: str) -> bool:
    """True if ``url`` answers HTTP 200."""
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - local URL
            return bool(response.status == 200)
    except (OSError, ValueError):  # refused, reset, timeout, HTTP errors
        return False


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
    # The edge proxy takes localhost:8080 for every tenant host (acme.localhost:8080).
    router: dict[str, Any] = {"service": {"type": "ClusterIP", "port": 8000}}
    if env.get("ROUTER_REPLICAS"):
        router["replicas"] = int(env["ROUTER_REPLICAS"])
    return {
        "image": {"tag": TAG},
        "secrets": {
            "adminToken": env.get("ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN),
            "routerToken": env.get("ROUTER_TOKEN", "dev-router-token"),
            "metricsToken": env.get("ROUTER_METRICS_TOKEN", "dev-metrics-token"),
            "cellCredentials": {cell: cell_token(env, cell) for cell in seeds},
        },
        "controlPlane": control_plane,
        "router": router,
        "edge": {"enabled": True, "service": {"type": "LoadBalancer", "port": ROUTER_PORT}},
        "sessions": {"issuer": SESSION_ISSUER, "baseDomain": TENANT_BASE_DOMAIN},
        "redis": {"enabled": env.get("RATE_LIMIT_BACKEND") == "redis"},
    }


def dev_idp_values() -> dict[str, Any]:
    return {"issuer": SESSION_ISSUER, "baseDomain": TENANT_BASE_DOMAIN}


def mgr_tag(root: Path, git: Git, now: float) -> str:
    return image_tag(source_version(root / MGR_VERSION_FILE), git, now)


def build_mgr(root: Path, run: Runner, env: Mapping[str, str], tag: str) -> int:
    return run(["docker", "build", "--target", MGR, "-t", f"cellarch/{MGR}:{tag}", str(root)], env)


def install_mgr(clusters: ClusterToolkit, root: Path, cell: str, tag: str) -> None:
    clusters.import_images(cell, [f"cellarch/{MGR}:{tag}"])
    values = {"cellId": cell, "image": {"tag": tag}}
    clusters.helm_upgrade(cell, MGR, root / "charts" / MGR, values)


def deploy_mgr(
    root: Path, run: Runner, clusters: ClusterToolkit, env: Mapping[str, str], tag: str
) -> int:
    """Ship mgr alone: build it, then upgrade only its release in every running cell."""
    cells = sorted(c for c, s in clusters.clusters().items() if c != PLATFORM and s == "running")
    if not cells:
        print("error: no running cell clusters; start them with `make up`", file=sys.stderr)
        return 1
    print(f"Building mgr {tag}...")
    code = build_mgr(root, run, env, tag)
    if code:
        return code
    for cell in cells:
        print(f"Upgrading mgr in {cell}...")
        install_mgr(clusters, root, cell, tag)
    write_version(root, MGR, tag)
    print(f"mgr {tag} is running in {', '.join(cells)}.")
    return 0


def build_images(root: Path, run: Runner, env: Mapping[str, str]) -> int:
    for name in IMAGES:
        code = run(["docker", "build", "--target", name, "-t", image(name), str(root)], env)
        if code:
            return code
    return 0


def up(
    root: Path,
    run: Runner,
    clusters: ClusterToolkit,
    env: Mapping[str, str],
    port_check: Callable[[int], bool],
    probe: Callable[[str], bool] = answers,
    sleep: Callable[[float], None] = time.sleep,
    tag: str = "",
) -> int:
    seeds = seed_cells(env)
    existing = clusters.clusters()
    if PLATFORM not in existing and any(port_check(p) for p in (ROUTER_PORT, CONTROL_PLANE_PORT)):
        print(
            f"error: ports {ROUTER_PORT}/{CONTROL_PLANE_PORT} are in use by another program, "
            "probably the old Docker Compose stack. Stop it first:\n"
            "  docker compose -p cellarch down",
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
    code = build_images(root, run, env) or build_mgr(root, run, env, tag)
    if code:
        return code

    print("Loading images and installing the charts...")
    clusters.import_images(PLATFORM, [image(i) for i in PLATFORM_IMAGES])
    clusters.import_images(PLATFORM, [image(DEV_IDP)])
    for cell in cells:
        clusters.import_images(cell, [image(i) for i in CELL_IMAGES])
        if cell in seeds:
            values: dict[str, Any] = {
                "cellId": cell,
                "secrets": {"internalToken": cell_token(env, cell)},
            }
            if env.get("MAX_CONCURRENT_REQUESTS"):
                values["gateway"] = {"maxConcurrentRequests": int(env["MAX_CONCURRENT_REQUESTS"])}
            clusters.helm_upgrade(cell, RELEASE, root / "charts" / "cell", values)
        else:  # deployed with cellctl: keep its generated credential
            clusters.helm_upgrade(cell, RELEASE, root / "charts" / "cell", {}, reuse_values=True)
        install_mgr(clusters, root, cell, tag)
    write_version(root, MGR, tag)
    clusters.set_cell_dns({cell: clusters.node_ip(cell) for cell in cells})
    clusters.helm_upgrade(PLATFORM, DEV_IDP, root / "charts" / DEV_IDP, dev_idp_values())
    clusters.helm_upgrade(
        PLATFORM, RELEASE, root / "charts" / "platform", platform_values(env, seeds)
    )
    # helm --wait covers our pods, not k3s's load balancer in front of them.
    health = f"http://127.0.0.1:{CONTROL_PLANE_PORT}/healthz"
    waited = 0.0
    while not probe(health):
        if waited >= READY_TIMEOUT_SECONDS:
            print(
                f"error: the control plane did not answer at {health} within "
                f"{READY_TIMEOUT_SECONDS}s; see `kubectl --context k3d-cellarch-platform "
                "get pods -A`",
                file=sys.stderr,
            )
            return 1
        sleep(READY_POLL_SECONDS)
        waited += READY_POLL_SECONDS
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
    clusters: ClusterToolkit | None = None,
    port_in_use: Callable[[int], bool] = port_in_use,
    answers: Callable[[str], bool] = answers,
    sleep: Callable[[float], None] = time.sleep,
    git: Git | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = args[0] if args else "help"
    run = run or _process_runner(root)
    k3d: ClusterToolkit = clusters or LocalClusters()

    if target == "help":
        print(HELP)
        return 0
    targets = ("up", "deploy-mgr", "down", "clean", "status", "build", "test", "setup", "token")
    if target not in targets:
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
    tag = mgr_tag(root, git or _git_runner(root), now())
    if target == "build":
        return build_images(root, run, env) or build_mgr(root, run, env, tag)
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
            with contextlib.suppress(ClusterError):  # no network: nothing to remove
                k3d.delete_network()
            print("Clusters and their data deleted.")
            return 0

        code = ensure_venv(root, run)
        if code:
            return code
        if target == "test":
            return run([str(venv_python(root)), "-m", "pytest", "--cov"], env)
        if target == "up":
            return up(root, run, k3d, env, port_in_use, answers, sleep, tag)
        if target == "deploy-mgr":
            return deploy_mgr(root, run, k3d, env, tag)
        return run([str(venv_python(root)), "-m", "cellarch.cli.cellctl", "status"], env)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ClusterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _git_runner(root: Path) -> Git:  # pragma: no cover - real processes
    def git(cmd: Sequence[str]) -> str:
        try:
            # Fixed git commands built by this script, never from user input.
            done = subprocess.run(list(cmd), cwd=root, capture_output=True, text=True, check=True)  # noqa: S603
        except (OSError, subprocess.CalledProcessError) as exc:
            raise OSError(str(exc)) from exc
        return done.stdout

    return git


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
