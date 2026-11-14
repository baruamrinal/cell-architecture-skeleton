"""Moving a tenant to another cell, step by step (docs/plan.md §5).

The control plane runs each step of the move procedure; the operator's tool installs
and removes the tenant's stack in between, because the control plane never starts
containers:

1. start: the tenant is frozen on the source, its database is created on the target;
2. install the tenant's stack in the target cell's cluster;
3. copy the data (retried while the target stack is still creating its tables);
4. switch the route to the target (retried until the target stack is ready);
5. uninstall the stack on the source, then complete: the source drops its copy.

A failure before the switch rolls the move back: the target stack is uninstalled and
the control plane unfreezes the source. After the switch the move only goes forward.
Locally this runs in ``cellctl``; on AWS in the deployment pipeline (stage 7).
"""

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cellarch.cli.api import AdminApi, CommandError
from cellarch.cli.clusters import ClusterError
from cellarch.cli.onboarding import (
    ACTIVATION_ATTEMPTS,
    ACTIVATION_DELAY_SECONDS,
    TENANT_CHART,
    Progress,
    StackClusters,
    require_local_cell,
    tenant_release,
)

NOT_READY = "tenant_stack_not_ready"


def _until_ready(api: AdminApi, path: str, sleep: Callable[[float], None]) -> Any:
    """Call a move step, retrying while the target stack isn't ready yet."""
    for attempt in range(1, ACTIVATION_ATTEMPTS + 1):
        try:
            return api.call("POST", path)
        except CommandError as exc:
            if exc.code != NOT_READY or attempt == ACTIVATION_ATTEMPTS:
                raise
            sleep(ACTIVATION_DELAY_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def move(
    api: AdminApi,
    clusters: StackClusters,
    charts_dir: Path,
    tenant_id: str,
    target: str | None,
    *,
    progress: Progress,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Move the tenant to ``target`` (or a cell placement chooses); returns the move."""
    tenant = api.call("GET", f"/v1/tenants/{tenant_id}")
    require_local_cell(clusters, tenant["cell_id"])
    if target is not None:
        require_local_cell(clusters, target)
    body = {"target_cell_id": target} if target else {}
    started = api.call("POST", f"/v1/tenants/{tenant_id}/moves", body)
    move_id, source, target = (
        started["move_id"],
        started["source_cell_id"],
        started["target_cell_id"],
    )
    release = tenant_release(tenant_id)
    progress(f"Moving {tenant_id} from {source} to {target} ({move_id}); writes are paused.")
    installed = False
    try:
        require_local_cell(clusters, target)
        progress(f"Starting {tenant_id}'s services in {target}'s cluster...")
        installed = True
        clusters.helm_upgrade(
            target,
            release,
            charts_dir / TENANT_CHART,
            {"tenantId": tenant_id, "tier": tenant["tier"]},
        )
        progress(f"Copying {tenant_id}'s data to {target}...")
        copied = _until_ready(api, f"/v1/moves/{move_id}/copy", sleep)
        progress(f"Copied {copied['copied_rows']} rows. Switching {tenant_id} to {target}...")
        _until_ready(api, f"/v1/moves/{move_id}/switch", sleep)
    except (CommandError, ClusterError) as exc:
        progress(f"Moving {tenant_id} failed; rolling back...")
        if installed:
            with contextlib.suppress(ClusterError):
                clusters.helm_uninstall(target, release)
        reason = exc.code if isinstance(exc, CommandError) else "stack_install_failed"
        try:
            api.call("POST", f"/v1/moves/{move_id}/rollback", {"reason": reason})
        except CommandError as rollback_error:
            progress(
                f"warning: rollback incomplete ({rollback_error.message}); "
                f"retry with: cellctl moves rollback {move_id}"
            )
        raise

    progress(f"{tenant_id} is served by {target}. Removing it from {source}...")
    try:
        clusters.helm_uninstall(source, release)
    except ClusterError as exc:
        progress(f"warning: could not stop the old services on {source}: {exc}")
    finished = api.call("POST", f"/v1/moves/{move_id}/complete")
    if finished["cleanup_pending"]:
        progress(
            f"warning: {source} still holds a copy of {tenant_id} (cleanup pending); "
            f"retry with: cellctl moves complete {move_id}"
        )
    else:
        progress(f"Tenant {tenant_id} moved to {target}.")
    return finished


def rollback(api: AdminApi, clusters: StackClusters, move_id: str, *, progress: Progress) -> Any:
    """Roll back a move that stopped before its switch (e.g. cellctl was interrupted)."""
    current = api.call("GET", f"/v1/moves/{move_id}")
    target = current["target_cell_id"]
    if current["state"] in ("started", "copied"):
        require_local_cell(clusters, target)
        progress(f"Stopping {current['tenant_id']}'s services in {target}'s cluster...")
        clusters.helm_uninstall(target, tenant_release(current["tenant_id"]))
    result = api.call("POST", f"/v1/moves/{move_id}/rollback", {"reason": "operator_rollback"})
    progress(f"Move {move_id} is {result['state']}.")
    return result
