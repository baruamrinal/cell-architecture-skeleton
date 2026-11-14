"""Control plane registry: cells, tenants, tenant -> cell assignment, moves, audit log."""

import json
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
-- Tenant moves (docs/plan.md §5). Kept after the tenant is gone, as history.
CREATE TABLE IF NOT EXISTS moves (
    move_id         TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    source_cell_id  TEXT NOT NULL,
    target_cell_id  TEXT NOT NULL,
    state           TEXT NOT NULL,
    cleanup_pending TEXT,            -- cell still holding a copy to delete
    error           TEXT,            -- why it was rolled back
    copied_rows     INTEGER,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    updated_by      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_moves_tenant ON moves (tenant_id);
-- Every admin change, including refused and failed ones.
CREATE TABLE IF NOT EXISTS audit_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    actor          TEXT NOT NULL,
    action         TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    error          TEXT,
    tenant_id      TEXT,
    cell_id        TEXT,
    target_cell_id TEXT,
    detail         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_events (tenant_id, event_id);
"""

# Columns added after the first release: (table, column, definition). Databases created
# before them are upgraded in place; existing rows get 'unknown' as the actor.
_ADDED_COLUMNS = [
    ("cells", "created_by", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("cells", "updated_at", "TEXT NOT NULL DEFAULT ''"),
    ("cells", "updated_by", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("tenants", "created_by", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("tenants", "updated_by", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("tenants", "rate_limit_rps", "REAL"),
    ("tenants", "rate_limit_burst", "INTEGER"),
]

# Moves that block another move of the same tenant.
UNFINISHED = ("started", "copied", "switched")

# incoming_tenants: moves heading to the cell that haven't switched yet (they hold a slot).
_CELLS_WITH_LOAD = """
SELECT c.*, COUNT(t.tenant_id) AS tenant_count,
       (SELECT COUNT(*) FROM moves m WHERE m.target_cell_id = c.cell_id
        AND m.state IN ('started', 'copied')) AS incoming_tenants
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
    ) -> dict[str, Any]:
        now = utcnow()
        try:
            with connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name, tier, placement_mode, cell_id, status, "
                    "created_at, updated_at, created_by, updated_by) "
                    "VALUES (?, ?, ?, 'pooled', ?, 'provisioning', ?, ?, ?, ?)",
                    (tenant_id, name, tier, cell_id, now, now, actor, actor),
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

    # --- moves ---------------------------------------------------------------------------

    def add_move(
        self, move_id: str, tenant_id: str, source: str, target: str, actor: str
    ) -> dict[str, Any]:
        """Record a new move and mark the tenant ``migrating``, in one transaction."""
        now = utcnow()
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO moves (move_id, tenant_id, source_cell_id, target_cell_id, state, "
                "created_at, created_by, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, 'started', ?, ?, ?, ?)",
                (move_id, tenant_id, source, target, now, actor, now, actor),
            )
            conn.execute(
                "UPDATE tenants SET status = 'migrating', updated_at = ?, updated_by = ? "
                "WHERE tenant_id = ?",
                (now, actor, tenant_id),
            )
        return self._require_move(move_id)

    def get_move(self, move_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM moves WHERE move_id = ?", (move_id,)).fetchone()
            return dict(row) if row else None

    def list_moves(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Newest first."""
        where, params = ("WHERE tenant_id = ?", (tenant_id,)) if tenant_id else ("", ())
        with connect(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM moves {where} ORDER BY created_at DESC, move_id",  # noqa: S608
                params,
            )
            return [dict(r) for r in rows.fetchall()]

    def unfinished_move(self, tenant_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM moves WHERE tenant_id = ? AND state IN (?, ?, ?)",
                (tenant_id, *UNFINISHED),
            ).fetchone()
            return dict(row) if row else None

    def update_move(self, move_id: str, actor: str, **fields: Any) -> dict[str, Any]:
        assert set(fields) <= {"state", "cleanup_pending", "error", "copied_rows"}  # noqa: S101
        assignments = "".join(f"{name} = ?, " for name in fields)
        with connect(self._db_path) as conn:
            conn.execute(
                f"UPDATE moves SET {assignments}updated_at = ?, updated_by = ? "  # noqa: S608
                "WHERE move_id = ?",
                (*fields.values(), utcnow(), actor, move_id),
            )
        return self._require_move(move_id)

    def end_move(
        self, move: dict[str, Any], actor: str, *, state: str, cell_id: str, **fields: Any
    ) -> dict[str, Any]:
        """Switch or roll back: the tenant is ``active`` on ``cell_id`` again."""
        now = utcnow()
        with connect(self._db_path) as conn:
            conn.execute(
                "UPDATE tenants SET cell_id = ?, status = 'active', updated_at = ?, "
                "updated_by = ? WHERE tenant_id = ?",
                (cell_id, now, actor, move["tenant_id"]),
            )
        return self.update_move(str(move["move_id"]), actor, state=state, **fields)

    def _require_move(self, move_id: str) -> dict[str, Any]:
        move = self.get_move(move_id)
        if move is None:  # pragma: no cover - only called right after a write
            raise KeyError(move_id)
        return move

    # --- audit log -----------------------------------------------------------------------

    def add_event(
        self,
        *,
        actor: str,
        action: str,
        outcome: str,
        error: str | None = None,
        tenant_id: str | None = None,
        cell_id: str | None = None,
        target_cell_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO audit_events (at, actor, action, outcome, error, tenant_id, "
                "cell_id, target_cell_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    utcnow(),
                    actor,
                    action,
                    outcome,
                    error,
                    tenant_id,
                    cell_id,
                    target_cell_id,
                    json.dumps(detail or {}, sort_keys=True),
                ),
            )

    def list_events(
        self,
        *,
        limit: int,
        before: int | None = None,
        tenant_id: str | None = None,
        cell_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest first; ``before`` (an event id) is where the previous page ended."""
        clauses: list[str] = []
        params: list[Any] = []
        if before is not None:
            clauses.append("event_id < ?")
            params.append(before)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if cell_id is not None:
            clauses.append("(cell_id = ? OR target_cell_id = ?)")
            params.extend([cell_id, cell_id])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_events {where} ORDER BY event_id DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

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
