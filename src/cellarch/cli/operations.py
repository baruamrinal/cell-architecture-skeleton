"""Promote, demote, drain and consolidate: workflows over the move procedure (docs/plan.md §5.1).

Each one is a sequence of tenant moves (moves.py) plus deploying or decommissioning a
cell (cells.py). They check what they can up front (room on other cells, active
tenants), so they don't stop half-way for a reason known in advance. A move that fails
is rolled back as usual; running the operation again carries on.
"""

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cellarch.cli.api import AdminApi, CommandError
from cellarch.cli.cells import GATEWAY_IMAGE, decommission_cell, deploy_cell
from cellarch.cli.clusters import ClusterError, ClusterToolkit
from cellarch.cli.moves import move
from cellarch.cli.onboarding import Progress

Sleep = Callable[[float], None]


def dedicated_cell_id(tenant_id: str) -> str:
    return f"cell-{tenant_id}"


def promote(
    api: AdminApi,
    clusters: ClusterToolkit,
    charts_dir: Path,
    tenant_id: str,
    *,
    cell_id: str | None = None,
    size: str = "small",
    image: str = GATEWAY_IMAGE,
    progress: Progress,
    sleep: Sleep = time.sleep,
) -> Any:
    """Give the tenant a dedicated cell of its own and move it there."""
    tenant = api.call("GET", f"/v1/tenants/{tenant_id}")
    if tenant["placement_mode"] == "dedicated":
        raise CommandError(
            "tenant_already_dedicated", f"{tenant_id} already has {tenant['cell_id']} to itself"
        )
    if tenant["status"] != "active":
        raise CommandError("tenant_not_active", f"{tenant_id} is {tenant['status']}")
    cell_id = cell_id or dedicated_cell_id(tenant_id)
    body = {"cell_id": cell_id, "type": "dedicated", "size": size}
    deploy_cell(api, clusters, charts_dir, body, image=image, progress=progress, sleep=sleep)
    try:
        return move(api, clusters, charts_dir, tenant_id, cell_id, progress=progress, sleep=sleep)
    except (CommandError, ClusterError):
        progress(f"Promotion failed; removing {cell_id}...")
        with contextlib.suppress(CommandError, ClusterError):
            decommission_cell(api, clusters, cell_id, delete_data=True, progress=progress)
        raise


def demote(
    api: AdminApi,
    clusters: ClusterToolkit,
    charts_dir: Path,
    tenant_id: str,
    *,
    delete_data: bool = False,
    progress: Progress,
    sleep: Sleep = time.sleep,
) -> Any:
    """Move the tenant back to a shared cell, then decommission its dedicated cell."""
    tenant = api.call("GET", f"/v1/tenants/{tenant_id}")
    old_cell = tenant["cell_id"]
    if api.call("GET", f"/v1/cells/{old_cell}")["type"] != "dedicated":
        raise CommandError(
            "tenant_not_dedicated", f"{tenant_id} is on shared cell {old_cell}; nothing to demote"
        )
    moved = move(api, clusters, charts_dir, tenant_id, None, progress=progress, sleep=sleep)
    decommission_cell(api, clusters, old_cell, delete_data=delete_data, progress=progress)
    return moved


def _room_elsewhere(cells: list[dict[str, Any]], cell_id: str) -> int:
    return sum(
        max(0, c["capacity"] - c["tenant_count"] - c["incoming_tenants"])
        for c in cells
        if c["cell_id"] != cell_id and c["type"] == "shared" and c["status"] == "active"
    )


def drain(
    api: AdminApi,
    clusters: ClusterToolkit,
    charts_dir: Path,
    cell_id: str,
    *,
    progress: Progress,
    sleep: Sleep = time.sleep,
) -> dict[str, Any]:
    """Stop placing tenants on the cell, then move every tenant it hosts elsewhere."""
    cell = api.call("GET", f"/v1/cells/{cell_id}")
    tenants = [t for t in api.call("GET", "/v1/tenants") if t["cell_id"] == cell_id]
    busy = sorted(t["tenant_id"] for t in tenants if t["status"] != "active")
    if busy:
        raise CommandError(
            "tenant_not_active", f"{cell_id} hosts tenants that aren't active: {', '.join(busy)}"
        )
    room = _room_elsewhere(api.call("GET", "/v1/cells"), cell_id)
    if room < len(tenants):
        raise CommandError(
            "no_capacity",
            f"{cell_id} hosts {len(tenants)} tenants but the other cells have room for {room}; "
            "deploy a cell or raise a capacity first",
            503,
        )
    if cell["status"] == "active":
        api.call("PATCH", f"/v1/cells/{cell_id}", {"status": "draining"})
    progress(f"{cell_id} is draining: no new tenants; moving {len(tenants)} out...")

    moved: list[str] = []
    for tenant in tenants:
        try:
            move(
                api, clusters, charts_dir, tenant["tenant_id"], None, progress=progress, sleep=sleep
            )
        except (CommandError, ClusterError):
            left = [t["tenant_id"] for t in tenants if t["tenant_id"] not in moved]
            progress(
                f"Drain stopped; still on {cell_id}: {', '.join(left)}. Run it again to continue."
            )
            raise
        moved.append(tenant["tenant_id"])
    progress(f"{cell_id} is empty.")
    return {"cell_id": cell_id, "status": "draining", "moved": moved}


def consolidate(
    api: AdminApi,
    clusters: ClusterToolkit,
    charts_dir: Path,
    cell_id: str,
    *,
    delete_data: bool = False,
    progress: Progress,
    sleep: Sleep = time.sleep,
) -> dict[str, Any]:
    """Drain a shared cell, then decommission it."""
    if api.call("GET", f"/v1/cells/{cell_id}")["type"] != "shared":
        raise CommandError("cell_not_shared", f"{cell_id} is dedicated; demote its tenant instead")
    drained = drain(api, clusters, charts_dir, cell_id, progress=progress, sleep=sleep)
    removed = decommission_cell(api, clusters, cell_id, delete_data=delete_data, progress=progress)
    return {**drained, **removed}
