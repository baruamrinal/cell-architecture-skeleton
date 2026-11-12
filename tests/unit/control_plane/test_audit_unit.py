from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cellarch.control_plane.app import actor_label
from cellarch.control_plane.store import ControlPlaneStore

OLD_SCHEMA = """
CREATE TABLE cells (
    cell_id TEXT PRIMARY KEY, url TEXT NOT NULL, type TEXT NOT NULL, hosting TEXT NOT NULL,
    size TEXT NOT NULL, status TEXT NOT NULL, capacity INTEGER NOT NULL, wave INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE tenants (
    tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, tier TEXT NOT NULL,
    placement_mode TEXT NOT NULL, cell_id TEXT NOT NULL REFERENCES cells(cell_id),
    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO cells VALUES ('cell-1', 'http://cell-1', 'shared', 'vendor', 'small', 'active',
                          4, 1, '2026-11-01T00:00:00+00:00');
INSERT INTO tenants VALUES ('acme', 'Acme', 'standard', 'pooled', 'cell-1', 'active',
                            '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00');
"""


def test_databases_from_before_the_audit_fields_are_migrated(tmp_path: Path) -> None:
    db = tmp_path / "cp.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(OLD_SCHEMA)

    store = ControlPlaneStore(str(db))

    cell = store.get_cell("cell-1")
    tenant = store.get_tenant("acme")
    assert cell is not None and tenant is not None
    assert (cell["created_by"], cell["updated_by"]) == ("unknown", "unknown")
    assert cell["updated_at"] == "2026-11-01T00:00:00+00:00"
    assert (tenant["created_by"], tenant["updated_by"]) == ("unknown", "unknown")
    ControlPlaneStore(str(db))  # migrating twice is harmless


def test_seeded_cells_are_created_by_config(tmp_path: Path) -> None:
    store = ControlPlaneStore(str(tmp_path / "cp.db"))
    store.seed_cell("cell-1", "http://cell-1", capacity=4)
    cell = store.get_cell("cell-1")
    assert cell is not None
    assert (cell["created_by"], cell["updated_by"]) == ("config", "config")


def test_update_without_changes_keeps_the_last_editor(tmp_path: Path) -> None:
    store = ControlPlaneStore(str(tmp_path / "cp.db"))
    store.seed_cell("cell-1", "http://cell-1", capacity=4)
    store.update_cell("cell-1", actor="admin:someone")
    cell = store.get_cell("cell-1")
    assert cell is not None and cell["updated_by"] == "config"


@pytest.mark.parametrize(
    ("role", "actor", "label"),
    [
        ("admin", "mrinal", "admin:mrinal"),
        ("admin", None, "admin"),
        ("admin", "", "admin"),
        ("admin", "ci.deploy@github", "admin:ci.deploy@github"),
    ],
)
def test_actor_label(role: str, actor: str | None, label: str) -> None:
    assert actor_label(role, actor) == label


@pytest.mark.parametrize("actor", ["has space", "x" * 65, "a;b", "über", "a/b"])
def test_invalid_actor_names(actor: str) -> None:
    with pytest.raises(ValueError, match="invalid caller name"):
        actor_label("admin", actor)
