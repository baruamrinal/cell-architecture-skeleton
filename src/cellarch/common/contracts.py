"""Wire contracts and identifiers shared across components."""

from typing import Literal

from pydantic import BaseModel, Field

from cellarch.common.ids import CELL_ID_PATTERN, TENANT_ID_PATTERN, is_valid_tenant_id

__all__ = ["CELL_ID_PATTERN", "TENANT_ID_PATTERN", "is_valid_tenant_id"]  # re-exported

CellType = Literal["shared", "dedicated"]
CellHosting = Literal["vendor", "customer"]
CellSize = Literal["small", "medium", "large"]
CellStatus = Literal["active", "draining", "disabled"]
TenantStatus = Literal["provisioning", "active", "migrating"]
PlacementMode = Literal["pooled", "dedicated"]
Tier = Literal["standard", "premium"]
# Does the tenant share database tables in its cell (pooled) or have its own database?


class RateLimit(BaseModel):
    """Token bucket: a steady rate plus a burst allowance (docs/plan.md §4)."""

    rps: float = Field(gt=0, le=1_000_000)  # requests per second, refill rate
    burst: int = Field(ge=1, le=10_000_000)  # bucket size: requests allowed at once


class RoutingEntry(BaseModel):
    """Where a tenant lives, and its limit: all the router needs from the control plane.

    The limit travels with the route, so it keeps being enforced from cached routes
    while the control plane is down.
    """

    tenant_id: str
    cell_id: str
    cell_url: str
    status: TenantStatus
    rate_limit: RateLimit | None = None
