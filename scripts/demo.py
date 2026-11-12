"""End-to-end walkthrough of the platform. Doubles as the compose smoke test.

    docker compose -f deploy/docker-compose.yml up --build -d
    python scripts/demo.py

Environment: ROUTER_URL (default http://localhost:8080), CONTROL_PLANE_URL
(default http://localhost:8000), ADMIN_TOKEN (default dev-admin-token).
Exits non-zero if any check fails.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx

TENANT_HEADER = "X-Tenant-ID"
CELL_HEADER = "X-Cell-ID"
INTERNAL_TOKEN_HEADER = "X-Internal-Token"  # noqa: S105 - a header name, not a secret
TENANTS = ("acme", "globex", "initech", "umbrella")


class DemoError(Exception):
    """A demo check failed."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoError(message)


def _step(title: str) -> None:
    print(f"\n== {title}")


def _onboard(admin: httpx.Client, tenant_id: str) -> dict[str, Any]:
    response = admin.post("/v1/tenants", json={"tenant_id": tenant_id, "name": tenant_id.title()})
    if response.status_code == 409:  # already onboarded by an earlier run
        response = admin.get(f"/v1/tenants/{tenant_id}")
    _check(response.status_code in (200, 201), f"onboarding {tenant_id}: {response.text}")
    tenant: dict[str, Any] = response.json()
    return tenant


def run_demo(admin: httpx.Client, router: httpx.Client) -> None:
    _step("Cells registered in the control plane")
    cells = admin.get("/v1/cells")
    _check(cells.status_code == 200, f"listing cells: {cells.text}")
    for cell in cells.json():
        print(f"  {cell['cell_id']:<8} {cell['status']:<8} capacity {cell['capacity']}")

    _step("Onboard tenants (fill-first placement)")
    placement = {}
    for tenant_id in TENANTS:
        tenant = _onboard(admin, tenant_id)
        placement[tenant_id] = tenant["cell_id"]
        print(f"  {tenant_id:<9} -> {tenant['cell_id']}")

    _step("Write through the router; each request lands on the tenant's cell")
    items = {}
    for tenant_id, cell_id in placement.items():
        response = router.post(
            "/v1/items", json={"name": f"{tenant_id} order"}, headers={TENANT_HEADER: tenant_id}
        )
        _check(response.status_code == 201, f"write for {tenant_id}: {response.text}")
        served_by = response.headers.get(CELL_HEADER)
        _check(served_by == cell_id, f"{tenant_id} served by {served_by}, expected {cell_id}")
        items[tenant_id] = response.json()["id"]
        print(f"  {tenant_id:<9} item {items[tenant_id][:16]}...  served by {served_by}")

    _step("Isolation: a tenant cannot read another tenant's item")
    response = router.get(f"/v1/items/{items['acme']}", headers={TENANT_HEADER: "globex"})
    _check(response.status_code == 404, f"globex read acme's item: HTTP {response.status_code}")
    print(f"  globex -> acme's item: HTTP {response.status_code} (expected 404)")

    _step("The router never exposes cells' /internal API")
    response = router.get(
        "/internal/tenants",
        headers={TENANT_HEADER: "acme", INTERNAL_TOKEN_HEADER: "anything"},
    )
    _check(response.status_code == 404, f"/internal via router: HTTP {response.status_code}")
    print(f"  GET /internal/tenants via router: HTTP {response.status_code} (expected 404)")

    print("\nDemo complete: all checks passed.")


def main() -> int:  # pragma: no cover - exercised by the compose smoke test
    router_url = os.environ.get("ROUTER_URL", "http://localhost:8080")
    control_plane_url = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
    token = os.environ.get("ADMIN_TOKEN", "dev-admin-token")
    with (
        httpx.Client(
            base_url=control_plane_url, headers={INTERNAL_TOKEN_HEADER: token}, timeout=10
        ) as admin,
        httpx.Client(base_url=router_url, timeout=10) as router,
    ):
        try:
            run_demo(admin, router)
        except (DemoError, httpx.HTTPError) as exc:
            print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
