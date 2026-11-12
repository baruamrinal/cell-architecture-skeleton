"""Tenant-scoped data access for a cell.

This is the single data-access layer for tenant data: every item query takes a
``tenant_id`` and filters on it. Nothing in the cell reads items any other way.
On AWS, Postgres row-level security adds a second line of defence (plan §10).
"""

import json
import sqlite3
import uuid
from typing import Any

from cellarch.common.db import connect, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id      TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    provisioned_at TEXT NOT NULL
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
        with connect(db_path) as conn:
            conn.executescript(SCHEMA)

    # --- tenants (managed by the control plane via /internal) ---------------------------

    def provision_tenant(self, tenant_id: str) -> dict[str, Any]:
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tenants (tenant_id, status, provisioned_at) "
                "VALUES (?, 'active', ?)",
                (tenant_id, utcnow()),
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
        with connect(self._db_path) as conn:
            return (
                conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,)).rowcount > 0
            )

    # --- items (tenant data; every query is scoped by tenant_id) ------------------------

    def create_item(self, tenant_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = f"itm_{uuid.uuid4().hex}"
        with connect(self._db_path) as conn:
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
        with connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE tenant_id = ? ORDER BY seq LIMIT ?", (tenant_id, limit)
            ).fetchall()
            return [_item(r) for r in rows]

    def get_item(self, tenant_id: str, item_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM items WHERE tenant_id = ? AND id = ?", (tenant_id, item_id)
            ).fetchone()
            return _item(row) if row else None

    def delete_item(self, tenant_id: str, item_id: str) -> bool:
        with connect(self._db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM items WHERE tenant_id = ? AND id = ?", (tenant_id, item_id)
            )
            return cursor.rowcount > 0
