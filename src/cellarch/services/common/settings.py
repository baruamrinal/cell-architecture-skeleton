"""Settings every tenant service instance needs: whose it is and how to reach its data."""

import os
from dataclasses import dataclass

from cellarch.common.config import require_env
from cellarch.common.contracts import is_valid_tenant_id


@dataclass(frozen=True)
class ServiceSettings:
    tenant_id: str  # the one tenant this instance serves
    stack_token: str  # shared by the tenant's gateway route and its sibling service
    db_path: str  # the tenant's database (SQLite locally; Postgres in the next stage)

    def __post_init__(self) -> None:
        if not is_valid_tenant_id(self.tenant_id):
            raise ValueError(f"TENANT_ID must be a valid tenant id, not {self.tenant_id!r}")

    @classmethod
    def env_values(cls) -> dict[str, str]:
        tenant_id = require_env("TENANT_ID")
        return {
            "tenant_id": tenant_id,
            "stack_token": require_env("STACK_TOKEN"),
            "db_path": os.environ.get("TENANT_DB_PATH", f"./data/tenant-{tenant_id}.db"),
        }

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        return cls(**cls.env_values())
