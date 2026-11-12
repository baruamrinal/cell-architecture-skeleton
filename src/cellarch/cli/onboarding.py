"""Tenant onboarding as a handshake (docs/plan.md §3.4).

The control plane never starts containers, so the operator's tool runs the steps:

1. the control plane places the tenant (``provisioning``) and its cell creates the
   tenant's database, logins and stack secret;
2. the tenant's stack (``tenant-stack`` chart) is installed in the cell's cluster;
3. the control plane activates the tenant once the cell sees the stack ready.

Any failure undoes every step done so far. Locally this runs in ``cellctl``; on AWS the
same steps run in the deployment pipeline (Argo CD installs the stack, stage 6).
"""

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from cellarch.cli.api import AdminApi, CommandError
from cellarch.cli.clusters import ClusterError

TENANT_CHART = "tenant-stack"
ACTIVATION_ATTEMPTS = 10
ACTIVATION_DELAY_SECONDS = 3.0

Progress = Callable[[str], None]


class StackClusters(Protocol):
    """The part of the cluster toolkit that installs tenant stacks."""

    def cell_ids(self) -> list[str]: ...

    def helm_upgrade(
        self,
        short: str,
        release: str,
        chart: Path,
        values: dict[str, Any],
        *,
        reuse_values: bool = False,
    ) -> None: ...

    def helm_uninstall(self, short: str, release: str) -> None: ...


def tenant_release(tenant_id: str) -> str:
    return f"tenant-{tenant_id}"


def _require_local_cell(clusters: StackClusters, cell_id: str) -> None:
    if cell_id not in clusters.cell_ids():
        raise CommandError(
            "cell_not_local",
            f"{cell_id} has no local cluster; its stacks are installed by its own pipeline",
        )


def onboard(
    api: AdminApi,
    clusters: StackClusters,
    charts_dir: Path,
    body: dict[str, Any],
    *,
    progress: Progress,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Run the onboarding handshake; returns the active tenant."""
    tenant = api.call("POST", "/v1/tenants", body)
    tenant_id, cell_id = tenant["tenant_id"], tenant["cell_id"]
    release = tenant_release(tenant_id)
    progress(f"Tenant {tenant_id} placed on {cell_id}; its database is ready.")
    installed = False
    try:
        _require_local_cell(clusters, cell_id)
        progress(f"Starting {tenant_id}'s services in {cell_id}'s cluster...")
        installed = True
        clusters.helm_upgrade(
            cell_id,
            release,
            charts_dir / TENANT_CHART,
            {"tenantId": tenant_id, "tier": tenant["tier"]},
        )
        for attempt in range(1, ACTIVATION_ATTEMPTS + 1):
            try:
                active = api.call("POST", f"/v1/tenants/{tenant_id}/activate")
            except CommandError as exc:
                if exc.code != "tenant_stack_not_ready" or attempt == ACTIVATION_ATTEMPTS:
                    raise
                sleep(ACTIVATION_DELAY_SECONDS)
            else:
                progress(f"Tenant {tenant_id} is active.")
                return active
    except (CommandError, ClusterError):
        progress(f"Onboarding {tenant_id} failed; undoing...")
        if installed:
            with contextlib.suppress(ClusterError):
                clusters.helm_uninstall(cell_id, release)
        with contextlib.suppress(CommandError):
            api.call("DELETE", f"/v1/tenants/{tenant_id}")
        raise
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def offboard(api: AdminApi, clusters: StackClusters, tenant_id: str, *, progress: Progress) -> None:
    """Stop the tenant's stack, then delete the tenant and its database."""
    tenant = api.call("GET", f"/v1/tenants/{tenant_id}")
    cell_id = tenant["cell_id"]
    _require_local_cell(clusters, cell_id)
    progress(f"Stopping {tenant_id}'s services in {cell_id}'s cluster...")
    clusters.helm_uninstall(cell_id, tenant_release(tenant_id))
    api.call("DELETE", f"/v1/tenants/{tenant_id}")
    progress(f"Tenant {tenant_id} and its data are deleted.")
