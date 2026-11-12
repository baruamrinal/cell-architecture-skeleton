"""Tenant-scoped data access for a cell.

This is the single data-access layer for tenant data: every item query takes a
``tenant_id`` and filters on it. Nothing in the cell reads items any other way.

Two isolation tiers (docs/plan.md §3.4):
  * ``pooled``   - the tenant's items share the cell's database with other pooled tenants
  * ``isolated`` - the tenant has its own database file in the cell (``tenants/<id>.db``)

Isolated databases still filter by ``tenant_id`` as a second safeguard. On AWS,
pooled tables get Postgres row-level security and isolated tenants get their own
database in the cell's Aurora cluster (stage 5).
"""

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from cellarch.common.contracts import is_valid_tenant_id
from cellarch.common.db import connect, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id      TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    provisioned_at TEXT NOT NULL,
    isolation      TEXT NOT NULL DEFAULT 'pooled'
);
CREATE TABLE IF NOT EXISTS items (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    seq        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_tenant ON items (tenant_id, seq);
"""

# A tenant's own database: just its items (the tenant registry stays in the cell's db).
ISOLATED_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    seq        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_tenant ON items (tenant_id, seq);
"""

ISOLATED = "isolated"
POOLED = "pooled"


def _item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "name": row["name"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }


class CellStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._tenant_dir = Path(db_path).parent / "tenants"
        with connect(db_path) as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tenants)")}
            if "isolation" not in columns:  # databases from before the isolation tier
                conn.execute(
                    "ALTER TABLE tenants ADD COLUMN isolation TEXT NOT NULL DEFAULT 'pooled'"
                )

    def _tenant_db(self, tenant_id: str) -> str:
        if not is_valid_tenant_id(tenant_id):  # it becomes a file name
            raise ValueError(f"unsafe tenant id for a database path: {tenant_id!r}")
        return str(self._tenant_dir / f"{tenant_id}.db")

    def _items_db(self, tenant_id: str) -> str:
        """Database holding this tenant's items."""
        tenant = self.get_tenant(tenant_id)
        if tenant is not None and tenant["isolation"] == ISOLATED:
            return self._tenant_db(tenant_id)
        return self._db_path

    # --- tenants (managed by the control plane via /internal) ---------------------------

    def provision_tenant(self, tenant_id: str, isolation: str = POOLED) -> dict[str, Any]:
        """Set up a tenant. Idempotent: provisioning again keeps the original isolation."""
        if isolation == ISOLATED:
            with connect(self._tenant_db(tenant_id)) as conn:
                conn.executescript(ISOLATED_SCHEMA)
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tenants (tenant_id, status, provisioned_at, isolation) "
                "VALUES (?, 'active', ?, ?)",
                (tenant_id, utcnow(), isolation),
            )
            row = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            return dict(row)

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            return dict(row) if row else None

    def list_tenants(self) -> list[dict[str, Any]]:
        with connect(self._db_path) as conn:
            rows = conn.execute("SELECT * FROM tenants ORDER BY tenant_id").fetchall()
            return [dict(r) for r in rows]

    def delete_tenant(self, tenant_id: str) -> bool:
        """Remove a tenant and all its data; an isolated tenant's database is deleted."""
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            return False
        with connect(self._db_path) as conn:
            conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
        if tenant["isolation"] == ISOLATED:
            database = Path(self._tenant_db(tenant_id))
            for path in (database, *(Path(f"{database}{s}") for s in ("-journal", "-wal", "-shm"))):
                path.unlink(missing_ok=True)
        return True

    # --- items (tenant data; every query is scoped by tenant_id) ------------------------

    def create_item(self, tenant_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = f"itm_{uuid.uuid4().hex}"
        with connect(self._items_db(tenant_id)) as conn:
            conn.execute(
                "INSERT INTO items (id, tenant_id, name, payload, created_at, seq) "
                "VALUES (?, ?, ?, ?, ?, "
                "(SELECT COALESCE(MAX(seq), 0) + 1 FROM items WHERE tenant_id = ?))",
                (item_id, tenant_id, name, json.dumps(payload), utcnow(), tenant_id),
            )
            row = conn.execute(
                "SELECT * FROM items WHERE tenant_id = ? AND id = ?", (tenant_id, item_id)
            ).fetchone()
            return _item(row)

    def list_items(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with connect(self._items_db(tenant_id)) as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE tenant_id = ? ORDER BY seq LIMIT ?", (tenant_id, limit)
            ).fetchall()
            return [_item(r) for r in rows]

    def get_item(self, tenant_id: str, item_id: str) -> dict[str, Any] | None:
        with connect(self._items_db(tenant_id)) as conn:
            row = conn.execute(
                "SELECT * FROM items WHERE tenant_id = ? AND id = ?", (tenant_id, item_id)
            ).fetchone()
            return _item(row) if row else None

    def delete_item(self, tenant_id: str, item_id: str) -> bool:
        with connect(self._items_db(tenant_id)) as conn:
            cursor = conn.execute(
                "DELETE FROM items WHERE tenant_id = ? AND id = ?", (tenant_id, item_id)
            )
            return cursor.rowcount > 0
