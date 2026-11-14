"""Deploying and decommissioning a cell: its own k3d cluster locally (docs/plan.md §8.5).

Used by ``cellctl cells deploy / decommission`` and by the operations built on them
(promote, demote, consolidate). On AWS the same steps trigger ``cell-lifecycle.yml``
(stage 7); the control plane never gets Docker or cluster access.
"""

import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cellarch.cli.api import AdminApi, CommandError
from cellarch.cli.clusters import ClusterError, ClusterToolkit, cell_url, cluster_name
from cellarch.cli.onboarding import Progress
from cellarch.common.contracts import is_valid_tenant_id

CELL_RELEASE = "cellarch"
GATEWAY_IMAGE = "cellarch/cell-gateway:dev"
# The control plane may not resolve a new cell's name until cluster DNS settles.
REGISTER_ATTEMPTS, REGISTER_DELAY_SECONDS = 21, 3.0
# The tenants' services run in every cell cluster (tenant-stack chart).
TENANT_IMAGES = ["cellarch/user-account:dev", "cellarch/order-processing:dev"]


class UsageError(Exception):
    """Invalid input detected after argument parsing (exit code 2)."""


def sync_cell_dns(clusters: ClusterToolkit, *, without: str | None = None) -> None:
    cells = [cell for cell in clusters.cell_ids() if cell != without]
    clusters.set_cell_dns({cell: clusters.node_ip(cell) for cell in cells})


def deploy_cell(
    api: AdminApi,
    clusters: ClusterToolkit,
    charts_dir: Path,
    body: dict[str, Any],
    *,
    image: str = GATEWAY_IMAGE,
    progress: Progress,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Create the cell's cluster, install the cell chart and register the cell.

    ``body`` is the registration (cell_id, type, size, capacity, wave). Any failure
    removes the cluster again.
    """
    cell_id = body["cell_id"]
    if not is_valid_tenant_id(cell_id):
        raise UsageError(
            f"invalid cell id {cell_id!r}: use 2-63 lowercase letters, digits and hyphens"
        )
    chart = charts_dir / "cell"
    if not chart.is_dir():
        raise UsageError(
            f"chart not found: {chart} (run from the project folder or pass --charts-dir)"
        )
    try:
        api.call("GET", f"/v1/cells/{cell_id}")
    except CommandError as exc:
        if exc.status != 404:
            raise
    else:
        raise CommandError("cell_exists", f"cell {cell_id} is already registered", 409)
    if cell_id in clusters.clusters():
        raise CommandError(
            "cell_cluster_exists",
            f"cluster {cluster_name(cell_id)} already exists; decommission it first",
        )

    credential = secrets.token_urlsafe(32)
    try:
        progress(f"Creating cluster {cluster_name(cell_id)}...")
        clusters.ensure_cluster(cell_id)
        clusters.import_images(cell_id, [image, *TENANT_IMAGES])
        progress("Installing the cell chart and waiting until the gateway is ready...")
        values = {"cellId": cell_id, "secrets": {"internalToken": credential}}
        clusters.helm_upgrade(cell_id, CELL_RELEASE, chart, values)
        sync_cell_dns(clusters)
        progress(f"Registering {cell_id} with the control plane...")
        registration = {**body, "url": cell_url(cell_id), "credential": credential}
        attempt = 1
        while True:
            try:
                cell = api.call("POST", "/v1/cells", registration)
                break
            except CommandError as exc:
                if exc.code != "cell_unreachable" or attempt == REGISTER_ATTEMPTS:
                    raise
                attempt += 1
                sleep(REGISTER_DELAY_SECONDS)
    except (CommandError, ClusterError):
        progress(f"Deploy failed; removing cluster {cluster_name(cell_id)}...")
        try:
            clusters.delete(cell_id)
            sync_cell_dns(clusters)
        except ClusterError as cleanup_error:
            progress(f"warning: cleanup incomplete: {cleanup_error}")
        raise
    progress(f"Cell {cell_id} is deployed in its own cluster and ready for tenants.")
    return cell


def decommission_cell(
    api: AdminApi,
    clusters: ClusterToolkit,
    cell_id: str,
    *,
    delete_data: bool,
    progress: Progress,
) -> dict[str, Any]:
    """Deregister an empty cell, then stop its cluster (or delete it with its data)."""
    api.call("DELETE", f"/v1/cells/{cell_id}")
    progress(f"{cell_id} deregistered from the control plane.")
    if cell_id not in clusters.clusters():
        progress(f"note: no local cluster {cluster_name(cell_id)}; nothing else to remove.")
        return {"cell_id": cell_id, "decommissioned": True, "cluster": "none"}

    if delete_data:
        clusters.delete(cell_id)
        outcome = "deleted"
    else:
        clusters.stop(cell_id)
        outcome = "stopped"
    # A stopped cluster is still listed, so leave this cell out of the DNS zone explicitly.
    sync_cell_dns(clusters, without=cell_id)
    progress(
        f"Cluster {cluster_name(cell_id)} "
        + ("deleted with its data." if outcome == "deleted" else "stopped; its data is kept.")
    )
    return {"cell_id": cell_id, "decommissioned": True, "cluster": outcome}
