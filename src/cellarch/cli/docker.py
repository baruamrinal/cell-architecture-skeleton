"""Local Docker runner and the compose definition for a cell deployed by cellctl.

cellctl drives Docker on the operator's machine; the control plane never does (giving
a container access to the Docker socket would give it root on the host). On AWS the
same `cellctl cells deploy` triggers the cell-lifecycle workflow instead (stage 6).
"""

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

COMPOSE_PROJECT = "cellarch"  # matches `name:` in deploy/docker-compose.yml
DEFAULT_IMAGE = "cellarch:dev"
CELL_PORT = 8000

Runner = Callable[..., subprocess.CompletedProcess[str]]


class DockerError(Exception):
    """A Docker command failed."""


class DockerRunner(Protocol):
    def compose(self, files: Sequence[Path], *args: str) -> None: ...
    def volume_exists(self, name: str) -> bool: ...
    def remove_volume(self, name: str) -> None: ...


class LocalDocker:
    def __init__(self, run: Runner = subprocess.run) -> None:
        self._run = run

    def compose(self, files: Sequence[Path], *args: str) -> None:
        file_args = [arg for f in files for arg in ("-f", str(f))]
        self._exec(["docker", "compose", *file_args, *args])

    def volume_exists(self, name: str) -> bool:
        return self._exec(["docker", "volume", "inspect", name], check=False).returncode == 0

    def remove_volume(self, name: str) -> None:
        self._exec(["docker", "volume", "rm", name])

    def _exec(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = self._run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise DockerError("docker CLI not found; install Docker and try again") from exc
        if check and result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            raise DockerError(f"`{' '.join(cmd[:3])}` failed: {output}")
        return result


def cell_compose_file(compose_file: Path, cell_id: str) -> Path:
    """Where cellctl keeps the generated compose file for a cell (git-ignored)."""
    return compose_file.parent / "cells" / f"{cell_id}.yml"


def volume_name(cell_id: str) -> str:
    return f"{COMPOSE_PROJECT}_{cell_id}-data"


def cell_url(cell_id: str) -> str:
    """Address the control plane and router use inside the compose network."""
    return f"http://{cell_id}:{CELL_PORT}"


def cell_compose_spec(cell_id: str, token: str, image: str = DEFAULT_IMAGE) -> dict[str, Any]:
    """Compose definition for one cell, hardened like the cells in docker-compose.yml.

    Written as JSON, which is valid YAML, so it needs no YAML library.
    """
    volume = f"{cell_id}-data"
    return {
        "services": {
            cell_id: {
                "image": image,
                "command": [
                    "uvicorn",
                    "cellarch.data_plane.cell.app:create_app",
                    "--factory",
                    "--host",
                    "0.0.0.0",  # noqa: S104 - inside the container; not published
                    "--port",
                    str(CELL_PORT),
                ],
                "environment": {
                    "CELL_ID": cell_id,
                    "CELL_DB_PATH": f"/data/{cell_id}.db",
                    "CELL_INTERNAL_TOKEN": token,
                },
                "volumes": [f"{volume}:/data"],
                "networks": ["platform"],
                "read_only": True,
                "tmpfs": ["/tmp"],  # noqa: S108 - container-private tmpfs
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "restart": "unless-stopped",
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "import urllib.request; "
                        f"urllib.request.urlopen('http://localhost:{CELL_PORT}/healthz')",
                    ],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 20,
                },
            }
        },
        "volumes": {volume: {}},
    }
