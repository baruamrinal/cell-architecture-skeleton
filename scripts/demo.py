"""End-to-end walkthrough of the platform. Doubles as the k3d smoke test.

    . .\\make up            (macOS/Linux: make up)
    python scripts/demo.py

Onboards tenants the way `cellctl tenants create` does (each gets its own services and
database in its cell), then signs up users and places orders through the router.

Environment: ROUTER_URL (default http://localhost:8080), CONTROL_PLANE_URL
(default http://localhost:8000), ADMIN_TOKEN (default dev-admin-token).
Exits non-zero if any check fails.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

TENANT_HEADER = "X-Tenant-ID"
CELL_HEADER = "X-Cell-ID"
INTERNAL_TOKEN_HEADER = "X-Internal-Token"  # noqa: S105 - a header name, not a secret
TENANTS = ("acme", "globex", "initech", "umbrella")

Onboard = Callable[[str], dict[str, Any]]


class DemoError(Exception):
    """A demo check failed."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoError(message)


def _step(title: str) -> None:
    print(f"\n== {title}")


def _ensure_tenant(admin: httpx.Client, onboard: Onboard, tenant_id: str) -> dict[str, Any]:
    existing = admin.get(f"/v1/tenants/{tenant_id}")
    if existing.status_code == 200:  # onboarded by an earlier run
        tenant: dict[str, Any] = existing.json()
        _check(
            tenant["status"] == "active",
            f"{tenant_id} is {tenant['status']}; delete it (cellctl tenants delete) and rerun",
        )
        return tenant
    return onboard(tenant_id)


def run_demo(admin: httpx.Client, router: httpx.Client, onboard: Onboard) -> None:
    run = uuid.uuid4().hex[:6]  # fresh emails on every run

    _step("Cells registered in the control plane")
    cells = admin.get("/v1/cells")
    _check(cells.status_code == 200, f"listing cells: {cells.text}")
    for cell in cells.json():
        print(f"  {cell['cell_id']:<8} {cell['status']:<8} capacity {cell['capacity']}")

    _step("Onboard tenants: placed fill-first, each with its own services and database")
    placement = {}
    for tenant_id in TENANTS:
        tenant = _ensure_tenant(admin, onboard, tenant_id)
        placement[tenant_id] = tenant["cell_id"]
        print(f"  {tenant_id:<9} -> {tenant['cell_id']}  ({tenant['status']})")

    _step("Sign up a user and place an order through the router")
    users = {}
    for tenant_id, cell_id in placement.items():
        headers = {TENANT_HEADER: tenant_id}
        user = router.post(
            "/v1/users",
            json={"email": f"ana-{run}@{tenant_id}.test", "name": "Ana"},
            headers={**headers, "Idempotency-Key": f"signup-{run}"},
        )
        _check(user.status_code == 201, f"sign-up for {tenant_id}: {user.text}")
        served_by = user.headers.get(CELL_HEADER)
        _check(served_by == cell_id, f"{tenant_id} served by {served_by}, expected {cell_id}")
        users[tenant_id] = user.json()["id"]
        order = router.post(
            "/v1/orders",
            json={
                "user_id": users[tenant_id],
                "lines": [{"sku": "book-1", "quantity": 2, "unit_price_cents": 1250}],
            },
            headers=headers,
        )
        _check(order.status_code == 201, f"order for {tenant_id}: {order.text}")
        total = order.json()["total_cents"]
        _check(total == 2500, f"{tenant_id} order total {total}, expected 2500")
        print(
            f"  {tenant_id:<9} user {users[tenant_id][:12]}...  order 25.00  served by {served_by}"
        )

    _step("Isolation: a tenant cannot see another tenant's users")
    response = router.get(f"/v1/users/{users['acme']}", headers={TENANT_HEADER: "globex"})
    _check(response.status_code == 404, f"globex read acme's user: HTTP {response.status_code}")
    print(f"  globex -> acme's user: HTTP {response.status_code} (expected 404)")
    response = router.post(
        "/v1/orders",
        json={
            "user_id": users["acme"],
            "lines": [{"sku": "x", "quantity": 1, "unit_price_cents": 1}],
        },
        headers={TENANT_HEADER: "globex"},
    )
    _check(
        response.status_code == 422, f"globex ordered for acme's user: HTTP {response.status_code}"
    )
    print(f"  globex orders for acme's user: HTTP {response.status_code} (expected 422)")

    _step("The router never exposes cells' /internal API")
    response = router.get(
        "/internal/tenants",
        headers={TENANT_HEADER: "acme", INTERNAL_TOKEN_HEADER: "anything"},
    )
    _check(response.status_code == 404, f"/internal via router: HTTP {response.status_code}")
    print(f"  GET /internal/tenants via router: HTTP {response.status_code} (expected 404)")

    print("\nDemo complete: all checks passed.")


def main() -> int:  # pragma: no cover - exercised by the k3d smoke test
    from cellarch.cli.api import Api, CommandError
    from cellarch.cli.clusters import ClusterError, LocalClusters
    from cellarch.cli.onboarding import onboard

    router_url = os.environ.get("ROUTER_URL", "http://localhost:8080")
    control_plane_url = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
    token = os.environ.get("ADMIN_TOKEN", "dev-admin-token")
    charts = Path(__file__).resolve().parents[1] / "charts"
    with (
        httpx.Client(
            base_url=control_plane_url, headers={INTERNAL_TOKEN_HEADER: token}, timeout=30
        ) as admin,
        httpx.Client(base_url=router_url, timeout=30) as router,
    ):
        api, clusters = Api(admin, token, "demo"), LocalClusters()

        def onboard_tenant(tenant_id: str) -> dict[str, Any]:
            body = {"tenant_id": tenant_id, "name": tenant_id.title(), "tier": "standard"}
            tenant: dict[str, Any] = onboard(
                api, clusters, charts, body, progress=lambda m: print(f"    {m}")
            )
            return tenant

        try:
            run_demo(admin, router, onboard_tenant)
        except (DemoError, httpx.HTTPError, CommandError, ClusterError) as exc:
            print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
