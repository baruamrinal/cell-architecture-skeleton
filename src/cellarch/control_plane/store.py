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
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'unknown',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    tier           TEXT NOT NULL,
    placement_mode TEXT NOT NULL,
    cell_id        TEXT NOT NULL REFERENCES cells(cell_id),
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL DEFAULT 'unknown',
    updated_by     TEXT NOT NULL DEFAULT 'unknown',
    isolation      TEXT NOT NULL DEFAULT 'pooled',
    -- Rate limit override; NULL means the tier default applies.
    rate_limit_rps   REAL,
    rate_limit_burst INTEGER
);
CREATE INDEX IF NOT EXISTS ix_tenants_cell ON tenants (cell_id);
-- Decommissioned cells, so configuration seeding doesn't bring them back on restart.
CREATE TABLE IF NOT EXISTS retired_cells (
    cell_id    TEXT PRIMARY KEY,
    retired_at TEXT NOT NULL
);
"""

# Columns added after the first release: (table, column, definition). Databases created
# before them are upgraded in place; existing rows get 'unknown' as the actor.
_ADDED_COLUMNS = [
    ("cells", "created_by", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("cells", "updated_at", "TEXT NOT NULL DEFAULT ''"),
    ("cells", "updated_by", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("tenants", "created_by", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("tenants", "updated_by", "TEXT NOT NULL DEFAULT 'unknown'"),
    # Tenants created before the isolation tier all live in the cell's shared database.
    ("tenants", "isolation", "TEXT NOT NULL DEFAULT 'pooled'"),
    ("tenants", "rate_limit_rps", "REAL"),
    ("tenants", "rate_limit_burst", "INTEGER"),
]

_CELLS_WITH_LOAD = """
SELECT c.*, COUNT(t.tenant_id) AS tenant_count,
       SUM(CASE WHEN t.isolation = 'isolated' THEN 1 ELSE 0 END) AS isolated_tenants
FROM cells c LEFT JOIN tenants t ON t.cell_id = c.cell_id
"""

# Actor recorded for changes made by the platform itself rather than an API caller.
SYSTEM_ACTOR = "system"


class DuplicateError(Exception):
    """The record already exists."""


class ControlPlaneStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        with connect(db_path) as conn:
            conn.executescript(SCHEMA)
            for table, column, definition in _ADDED_COLUMNS:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.execute("UPDATE cells SET updated_at = created_at WHERE updated_at = ''")

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
        actor: str = SYSTEM_ACTOR,
    ) -> dict[str, Any]:
        now = utcnow()
        try:
            with connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO cells (cell_id, url, type, hosting, size, status, capacity, "
                    "wave, created_at, created_by, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                    (cell_id, url, type, hosting, size, capacity, wave, now, actor, now, actor),
                )
                conn.execute("DELETE FROM retired_cells WHERE cell_id = ?", (cell_id,))
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(cell_id) from exc
        return self._require_cell(cell_id)

    def seed_cell(self, cell_id: str, url: str, capacity: int) -> None:
        """Register a cell from configuration without overwriting admin changes.

        Cells that were decommissioned are not brought back.
        """
        now = utcnow()
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO cells (cell_id, url, type, hosting, size, status, "
                "capacity, wave, created_at, created_by, updated_at, updated_by) "
                "SELECT ?, ?, 'shared', 'vendor', 'small', 'active', ?, 1, ?, 'config', ?, "
                "'config' WHERE NOT EXISTS (SELECT 1 FROM retired_cells WHERE cell_id = ?)",
                (cell_id, url, capacity, now, now, cell_id),
            )

    def delete_cell(self, cell_id: str) -> None:
        """Remove a cell (the caller checks it hosts no tenants) and remember it as retired."""
        with connect(self._db_path) as conn:
            conn.execute("DELETE FROM cells WHERE cell_id = ?", (cell_id,))
            conn.execute(
                "INSERT OR REPLACE INTO retired_cells (cell_id, retired_at) VALUES (?, ?)",
                (cell_id, utcnow()),
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
        self,
        cell_id: str,
        *,
        status: str | None = None,
        capacity: int | None = None,
        actor: str = SYSTEM_ACTOR,
    ) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            if status is not None:
                conn.execute("UPDATE cells SET status = ? WHERE cell_id = ?", (status, cell_id))
            if capacity is not None:
                conn.execute("UPDATE cells SET capacity = ? WHERE cell_id = ?", (capacity, cell_id))
            if status is not None or capacity is not None:
                conn.execute(
                    "UPDATE cells SET updated_at = ?, updated_by = ? WHERE cell_id = ?",
                    (utcnow(), actor, cell_id),
                )
        return self.get_cell(cell_id)

    def _require_cell(self, cell_id: str) -> dict[str, Any]:
        cell = self.get_cell(cell_id)
        if cell is None:  # pragma: no cover - only called right after an insert
            raise KeyError(cell_id)
        return cell

    # --- tenants -------------------------------------------------------------------------

    def add_tenant(
        self,
        tenant_id: str,
        name: str,
        tier: str,
        cell_id: str,
        actor: str = SYSTEM_ACTOR,
        isolation: str = "pooled",
    ) -> dict[str, Any]:
        now = utcnow()
        try:
            with connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name, tier, placement_mode, cell_id, status, "
                    "created_at, updated_at, created_by, updated_by, isolation) "
                    "VALUES (?, ?, ?, 'pooled', ?, 'provisioning', ?, ?, ?, ?, ?)",
                    (tenant_id, name, tier, cell_id, now, now, actor, actor, isolation),
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

    def set_tenant_status(self, tenant_id: str, status: str, actor: str = SYSTEM_ACTOR) -> None:
        self._update_tenant(tenant_id, "status", status, actor)

    def assign_tenant(self, tenant_id: str, cell_id: str, actor: str = SYSTEM_ACTOR) -> None:
        self._update_tenant(tenant_id, "cell_id", cell_id, actor)

    def set_rate_limit(
        self, tenant_id: str, rps: float | None, burst: int | None, actor: str = SYSTEM_ACTOR
    ) -> None:
        """Set a tenant's rate limit override, or clear it with ``None``."""
        with connect(self._db_path) as conn:
            conn.execute(
                "UPDATE tenants SET rate_limit_rps = ?, rate_limit_burst = ?, updated_at = ?, "
                "updated_by = ? WHERE tenant_id = ?",
                (rps, burst, utcnow(), actor, tenant_id),
            )

    def delete_tenant(self, tenant_id: str) -> None:
        with connect(self._db_path) as conn:
            conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))

    def _update_tenant(self, tenant_id: str, column: str, value: str, actor: str) -> None:
        assert column in ("status", "cell_id")  # noqa: S101 - never user-controlled
        with connect(self._db_path) as conn:
            conn.execute(
                f"UPDATE tenants SET {column} = ?, updated_at = ?, updated_by = ? "  # noqa: S608
                "WHERE tenant_id = ?",
                (value, utcnow(), actor, tenant_id),
            )

    # --- routing -------------------------------------------------------------------------

    def routing_entry(self, tenant_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT t.tenant_id, t.cell_id, c.url AS cell_url, t.status, t.tier, "
                "t.rate_limit_rps, t.rate_limit_burst "
                "FROM tenants t JOIN cells c ON c.cell_id = t.cell_id WHERE t.tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            return dict(row) if row else None
