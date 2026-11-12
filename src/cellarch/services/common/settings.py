"""Settings every tenant service instance needs: whose it is and how to reach its data."""

from dataclasses import dataclass

from cellarch.common.config import require_env
from cellarch.common.contracts import is_valid_tenant_id
from cellarch.common.sql import open_database


@dataclass(frozen=True)
class ServiceSettings:
    tenant_id: str  # the one tenant this instance serves
    stack_token: str  # shared by the tenant's gateway route and its sibling service
    # The tenant's database: postgresql://<tenant>_<service>@<cell db server>/tenant_<tenant>
    # in a cell (the login sees only this service's schema); sqlite:///<path> in tests.
    database_url: str

    def __post_init__(self) -> None:
        if not is_valid_tenant_id(self.tenant_id):
            raise ValueError(f"TENANT_ID must be a valid tenant id, not {self.tenant_id!r}")
        open_database(self.database_url)  # rejects unsupported URLs (without echoing them)

    @classmethod
    def env_values(cls) -> dict[str, str]:
        return {
            "tenant_id": require_env("TENANT_ID"),
            "stack_token": require_env("STACK_TOKEN"),
            "database_url": require_env("DATABASE_URL"),
        }

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        return cls(**cls.env_values())
