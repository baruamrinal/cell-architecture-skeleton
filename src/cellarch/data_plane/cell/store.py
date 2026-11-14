"""The cell gateway's registry of the tenants it hosts.

Each cell knows its own tenants (static stability: it never asks the control plane at
request time). A tenant is ``provisioning`` until its stack is running, then ``active``.
Tenant data lives in each tenant's own database (tenant_resources.py), never here.

The stack credential is kept so the gateway can call the tenant's services; on AWS it
comes from Secrets Manager instead (stage 6).
"""

from typing import Any

from cellarch.common.db import connect, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id      TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    provisioned_at TEXT NOT NULL,
    stack_token    TEXT
);
"""
_PUBLIC = "tenant_id, status, provisioned_at"


class CellStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        with connect(db_path) as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tenants)")}
            if "stack_token" not in columns:  # registries from before tenant stacks
                conn.execute("ALTER TABLE tenants ADD COLUMN stack_token TEXT")

    def add_tenant(self, tenant_id: str, stack_token: str) -> dict[str, Any]:
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tenants (tenant_id, status, provisioned_at, stack_token) "
                "VALUES (?, 'provisioning', ?, ?)",
                (tenant_id, utcnow(), stack_token),
            )
        tenant = self.get_tenant(tenant_id)
        if tenant is None:  # pragma: no cover - deleted concurrently
            raise LookupError(tenant_id)
        return tenant

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {_PUBLIC} FROM tenants WHERE tenant_id = ?",  # noqa: S608
                (tenant_id,),
            ).fetchone()
            return dict(row) if row else None

    def stack_token(self, tenant_id: str) -> str | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT stack_token FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            return row["stack_token"] if row else None

    def set_status(self, tenant_id: str, status: str) -> None:
        with connect(self._db_path) as conn:
            conn.execute("UPDATE tenants SET status = ? WHERE tenant_id = ?", (status, tenant_id))

    def list_tenants(self) -> list[dict[str, Any]]:
        with connect(self._db_path) as conn:
            rows = conn.execute(f"SELECT {_PUBLIC} FROM tenants ORDER BY tenant_id").fetchall()  # noqa: S608
            return [dict(r) for r in rows]

    def delete_tenant(self, tenant_id: str) -> bool:
        with connect(self._db_path) as conn:
            cursor = conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
            return cursor.rowcount > 0
