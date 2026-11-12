"""Local Kubernetes clusters for the platform and each cell (k3d + Helm + kubectl).

One cluster per cell plus a platform cluster, on a private Docker network
(docs/plan.md §8.4-8.5). Used by `make up` (scripts/dev.py) and `cellctl cells deploy`.
Standard library only: `make up` imports it before the virtual environment exists.

Lessons from probing k3d on Docker Desktop, built in here:
* each cluster's Kubernetes API is bound to 127.0.0.1 on its own port (the default,
  host.docker.internal, resolved to the LAN address and didn't connect);
* cell names resolve through a `cellarch.internal` zone in the platform cluster's
  `coredns-custom` ConfigMap (k3s rewrites its own host list, but leaves this alone);
* lean clusters: one node, no Traefik, no metrics-server, no extra load balancer.
"""

import json
import os
import socket
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

NETWORK = "cellarch"
PREFIX = "cellarch-"
PLATFORM = "platform"
DNS_ZONE = "cellarch.internal"
CELL_PORT = 8000


@dataclass(frozen=True)
class Result:
    code: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], str | None], Result]


class ClusterError(Exception):
    """A k3d, Helm, kubectl or Docker command failed."""


def run_process(cmd: Sequence[str], stdin: str | None = None) -> Result:
    completed = subprocess.run(  # noqa: S603 - commands are built here, never from user input
        list(cmd), input=stdin, capture_output=True, text=True, check=False
    )
    return Result(completed.returncode, completed.stdout, completed.stderr)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def cluster_name(short: str) -> str:
    return f"{PREFIX}{short}"


def context(short: str) -> str:
    return f"k3d-{cluster_name(short)}"


def cell_host(cell_id: str) -> str:
    return f"{cell_id}.{DNS_ZONE}"


def cell_url(cell_id: str) -> str:
    """Address the router and control plane use to reach a cell's gateway."""
    return f"http://{cell_host(cell_id)}:{CELL_PORT}"


class ClusterToolkit(Protocol):
    """What `make up` and `cellctl` need from the local cluster runtime."""

    def clusters(self) -> dict[str, str]: ...
    def cell_ids(self) -> list[str]: ...
    def ensure_network(self) -> None: ...
    def ensure_cluster(self, short: str, *, ports: Iterable[str] = ()) -> None: ...
    def stop(self, short: str) -> None: ...
    def delete(self, short: str) -> None: ...
    def delete_network(self) -> None: ...
    def import_images(self, short: str, images: Sequence[str]) -> None: ...
    def node_ip(self, short: str) -> str: ...
    def helm_upgrade(
        self,
        short: str,
        release: str,
        chart: Path,
        values: Mapping[str, Any],
        *,
        reuse_values: bool = False,
    ) -> None: ...
    def helm_uninstall(self, short: str, release: str) -> None: ...
    def set_cell_dns(self, entries: Mapping[str, str]) -> bool: ...


class LocalClusters:
    def __init__(
        self, run: Runner = run_process, free_port: Callable[[], int] = find_free_port
    ) -> None:
        self._run = run
        self._free_port = free_port

    def _exec(self, cmd: Sequence[str], stdin: str | None = None) -> str:
        try:
            result = self._run(cmd, stdin)
        except FileNotFoundError as exc:
            raise ClusterError(f"{cmd[0]} not found; install it and open a new terminal") from exc
        if result.code != 0:
            output = (result.stderr or result.stdout).strip().splitlines()
            raise ClusterError(f"`{' '.join(cmd[:3])}` failed: {' '.join(output[-3:])}")
        return result.stdout

    # --- clusters --------------------------------------------------------------------

    def ensure_network(self) -> None:
        names = self._exec(["docker", "network", "ls", "--format", "{{.Name}}"]).split()
        if NETWORK not in names:
            self._exec(["docker", "network", "create", NETWORK])

    def clusters(self) -> dict[str, str]:
        """Our clusters (short name -> "running" | "stopped")."""
        listed = json.loads(self._exec(["k3d", "cluster", "list", "-o", "json"]) or "[]")
        return {
            c["name"].removeprefix(PREFIX): ("running" if c["serversRunning"] else "stopped")
            for c in listed
            if c["name"].startswith(PREFIX)
        }

    def cell_ids(self) -> list[str]:
        return sorted(name for name in self.clusters() if name != PLATFORM)

    def ensure_cluster(self, short: str, *, ports: Iterable[str] = ()) -> None:
        state = self.clusters().get(short)
        if state == "running":
            return
        if state == "stopped":
            self._exec(["k3d", "cluster", "start", cluster_name(short)])
            return
        cmd = [
            "k3d", "cluster", "create", cluster_name(short),
            "--network", NETWORK,
            "--no-lb",
            "--api-port", f"127.0.0.1:{self._free_port()}",
            "--k3s-arg", "--disable=traefik@server:0",
            "--k3s-arg", "--disable=metrics-server@server:0",
            "--wait", "--timeout", "180s",
        ]  # fmt: skip
        for port in ports:
            cmd += ["-p", f"{port}@server:0:direct"]  # no LB: map on the node
        self._exec(cmd)

    def stop(self, short: str) -> None:
        self._exec(["k3d", "cluster", "stop", cluster_name(short)])

    def delete(self, short: str) -> None:
        self._exec(["k3d", "cluster", "delete", cluster_name(short)])

    def delete_network(self) -> None:
        self._exec(["docker", "network", "rm", NETWORK])

    def import_images(self, short: str, images: Sequence[str]) -> None:
        self._exec(["k3d", "image", "import", *images, "-c", cluster_name(short)])

    def node_ip(self, short: str) -> str:
        template = f'{{{{(index .NetworkSettings.Networks "{NETWORK}").IPAddress}}}}'
        node = f"k3d-{cluster_name(short)}-server-0"
        return self._exec(["docker", "inspect", node, "--format", template]).strip()

    # --- Helm ----------------------------------------------------------------------------

    def helm_upgrade(
        self,
        short: str,
        release: str,
        chart: Path,
        values: Mapping[str, Any],
        *,
        reuse_values: bool = False,
    ) -> None:
        """Install or upgrade a release. Values (they hold secrets) go in a temp file."""
        handle, path = tempfile.mkstemp(suffix=".json", prefix="cellarch-values-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(values, file)  # JSON is valid YAML
            cmd = [
                "helm", "upgrade", "--install", release, str(chart),
                "--kube-context", context(short),
                "-f", path,
                "--wait", "--timeout", "300s",
            ]  # fmt: skip
            if reuse_values:
                cmd.append("--reuse-values")
            self._exec(cmd)
        finally:
            Path(path).unlink(missing_ok=True)

    def helm_uninstall(self, short: str, release: str) -> None:
        """Remove a release and wait for its pods to go. Missing releases are fine."""
        self._exec(
            [
                "helm",
                "uninstall",
                release,
                "--kube-context",
                context(short),
                "--ignore-not-found",
                "--wait",
                "--timeout",
                "300s",
            ]  # fmt: skip
        )

    # --- cell DNS in the platform cluster ------------------------------------------------

    @staticmethod
    def dns_zone(entries: Mapping[str, str]) -> str:
        hosts = "\n".join(f"        {ip} {cell_host(cell)}" for cell, ip in sorted(entries.items()))
        return f"{DNS_ZONE}:53 {{\n    hosts {{\n{hosts}\n        fallthrough\n    }}\n}}\n"

    def set_cell_dns(self, entries: Mapping[str, str]) -> bool:
        """Point <cell>.cellarch.internal at each cell's node; returns whether it changed."""
        platform = context(PLATFORM)
        zone = self.dns_zone(entries)
        current = self._exec(
            [
                "kubectl", "--context", platform, "-n", "kube-system",
                "get", "configmap", "coredns-custom",
                "-o", "jsonpath={.data.cellarch\\.server}", "--ignore-not-found",
            ]
        )  # fmt: skip
        if current == zone:
            return False
        manifest = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "coredns-custom", "namespace": "kube-system"},
                "data": {"cellarch.server": zone},
            }
        )
        self._exec(["kubectl", "--context", platform, "apply", "-f", "-"], manifest)
        self._exec(
            [
                "kubectl", "--context", platform, "-n", "kube-system",
                "rollout", "restart", "deployment", "coredns",
            ]
        )  # fmt: skip
        return True
