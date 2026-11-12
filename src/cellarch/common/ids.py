"""Identifier rules for tenants and cells.

Standard library only: scripts/dev.py (`make up`) uses it before the virtual
environment exists. cellarch.common.contracts re-exports these.
"""

import re

TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
CELL_ID_PATTERN = TENANT_ID_PATTERN

_TENANT_ID = re.compile(TENANT_ID_PATTERN)


def is_valid_tenant_id(value: str) -> bool:
    return _TENANT_ID.fullmatch(value) is not None
