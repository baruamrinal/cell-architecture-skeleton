"""Wire contracts and identifiers shared across components."""

import re
from typing import Literal

from pydantic import BaseModel

TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
CELL_ID_PATTERN = TENANT_ID_PATTERN

CellType = Literal["shared", "dedicated"]
CellHosting = Literal["vendor", "customer"]
CellSize = Literal["small", "medium", "large"]
CellStatus = Literal["active", "draining", "disabled"]
TenantStatus = Literal["provisioning", "active", "migrating"]
PlacementMode = Literal["pooled", "dedicated"]
Tier = Literal["standard", "premium"]
# Does the tenant share database tables in its cell (pooled) or have its own database?
Isolation = Literal["pooled", "isolated"]

_TENANT_ID = re.compile(TENANT_ID_PATTERN)


def is_valid_tenant_id(value: str) -> bool:
    return _TENANT_ID.fullmatch(value) is not None


class RoutingEntry(BaseModel):
    """Where a tenant lives: the only thing the router needs from the control plane."""

    tenant_id: str
    cell_id: str
    cell_url: str
    status: TenantStatus
