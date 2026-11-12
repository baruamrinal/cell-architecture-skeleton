"""Control plane registry: cells, tenants and tenant -> cell assignment."""

import sqlite3
from typing import Any

from cellarch.common.db import connect, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    cell_id    TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    type       TEXT NOT NULL,
    hosting    TEXT NOT NULL,
    size       TEXT NOT NULL,
    status     TEXT NOT NULL,
    capacity   INTEGER NOT NULL,
    wave       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    tier           TEXT NOT NULL,
    placement_mode TEXT NOT NULL,
    cell_id        TEXT NOT NULL REFERENCES cells(cell_id),
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tenants_cell ON tenants (cell_id);
"""

_CELLS_WITH_LOAD = """
SELECT c.*, COUNT(t.tenant_id) AS tenant_count
FROM cells c LEFT JOIN tenants t ON t.cell_id = c.cell_id
"""


class DuplicateError(Exception):
    """The record already exists."""


class ControlPlaneStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        with connect(db_path) as conn:
            conn.executescript(SCHEMA)

    # --- cells ---------------------------------------------------------------------------

    def add_cell(
        self,
        *,
        cell_id: str,
        url: str,
        type: str,
        hosting: str,
        size: str,
        capacity: int,
        wave: int,
    ) -> dict[str, Any]:
        try:
            with connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO cells (cell_id, url, type, hosting, size, status, capacity, "
                    "wave, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (cell_id, url, type, hosting, size, capacity, wave, utcnow()),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(cell_id) from exc
        return self._require_cell(cell_id)

    def seed_cell(self, cell_id: str, url: str, capacity: int) -> None:
        """Register a cell from configuration without overwriting admin changes."""
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO cells (cell_id, url, type, hosting, size, status, "
                "capacity, wave, created_at) "
                "VALUES (?, ?, 'shared', 'vendor', 'small', 'active', ?, 1, ?)",
                (cell_id, url, capacity, utcnow()),
            )

    def get_cell(self, cell_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                f"{_CELLS_WITH_LOAD} WHERE c.cell_id = ? GROUP BY c.cell_id",  # noqa: S608
                (cell_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_cells(self) -> list[dict[str, Any]]:
        with connect(self._db_path) as conn:
            rows = conn.execute(f"{_CELLS_WITH_LOAD} GROUP BY c.cell_id ORDER BY c.cell_id")
            return [dict(r) for r in rows.fetchall()]

    def update_cell(
        self, cell_id: str, *, status: str | None = None, capacity: int | None = None
    ) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            if status is not None:
                conn.execute("UPDATE cells SET status = ? WHERE cell_id = ?", (status, cell_id))
            if capacity is not None:
                conn.execute("UPDATE cells SET capacity = ? WHERE cell_id = ?", (capacity, cell_id))
        return self.get_cell(cell_id)

    def _require_cell(self, cell_id: str) -> dict[str, Any]:
        cell = self.get_cell(cell_id)
        if cell is None:  # pragma: no cover - only called right after an insert
            raise KeyError(cell_id)
        return cell

    # --- tenants -------------------------------------------------------------------------

    def add_tenant(self, tenant_id: str, name: str, tier: str, cell_id: str) -> dict[str, Any]:
        now = utcnow()
        try:
            with connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name, tier, placement_mode, cell_id, status, "
                    "created_at, updated_at) VALUES (?, ?, ?, 'pooled', ?, 'provisioning', ?, ?)",
                    (tenant_id, name, tier, cell_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(tenant_id) from exc
        tenant = self.get_tenant(tenant_id)
        if tenant is None:  # pragma: no cover - only called right after an insert
            raise KeyError(tenant_id)
        return tenant

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            return dict(row) if row else None

    def list_tenants(self) -> list[dict[str, Any]]:
        with connect(self._db_path) as conn:
            rows = conn.execute("SELECT * FROM tenants ORDER BY tenant_id").fetchall()
            return [dict(r) for r in rows]

    def set_tenant_status(self, tenant_id: str, status: str) -> None:
        self._update_tenant(tenant_id, "status", status)

    def assign_tenant(self, tenant_id: str, cell_id: str) -> None:
        self._update_tenant(tenant_id, "cell_id", cell_id)

    def delete_tenant(self, tenant_id: str) -> None:
        with connect(self._db_path) as conn:
            conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))

    def _update_tenant(self, tenant_id: str, column: str, value: str) -> None:
        assert column in ("status", "cell_id")  # noqa: S101 - never user-controlled
        with connect(self._db_path) as conn:
            conn.execute(
                f"UPDATE tenants SET {column} = ?, updated_at = ? WHERE tenant_id = ?",  # noqa: S608
                (value, utcnow(), tenant_id),
            )

    # --- routing -------------------------------------------------------------------------

    def routing_entry(self, tenant_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT t.tenant_id, t.cell_id, c.url AS cell_url, t.status "
                "FROM tenants t JOIN cells c ON c.cell_id = t.cell_id WHERE t.tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            return dict(row) if row else None
