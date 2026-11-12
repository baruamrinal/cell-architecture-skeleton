from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cellarch.data_plane.cell.store import CellStore


@pytest.fixture
def store(tmp_path: Path) -> CellStore:
    return CellStore(str(tmp_path / "cell.db"))


def test_isolated_tenant_gets_its_own_database(store: CellStore, tmp_path: Path) -> None:
    tenant = store.provision_tenant("acme", isolation="isolated")
    store.create_item("acme", "a", {})

    assert tenant["isolation"] == "isolated"
    assert (tmp_path / "tenants" / "acme.db").is_file()
    assert [i["name"] for i in store.list_items("acme")] == ["a"]


def test_pooled_is_the_store_default(store: CellStore, tmp_path: Path) -> None:
    assert store.provision_tenant("globex")["isolation"] == "pooled"
    store.create_item("globex", "g", {})
    assert not (tmp_path / "tenants").exists()


def test_provisioning_again_keeps_the_original_isolation(store: CellStore) -> None:
    store.provision_tenant("acme", isolation="isolated")
    assert store.provision_tenant("acme", isolation="pooled")["isolation"] == "isolated"


def test_get_and_delete_items_in_an_isolated_database(store: CellStore) -> None:
    store.provision_tenant("acme", isolation="isolated")
    item = store.create_item("acme", "a", {"k": 1})
    assert store.get_item("acme", item["id"]) == item
    assert store.delete_item("acme", item["id"]) is True
    assert store.get_item("acme", item["id"]) is None


def test_deleting_an_isolated_tenant_removes_its_database(
    store: CellStore, tmp_path: Path
) -> None:
    store.provision_tenant("acme", isolation="isolated")
    store.create_item("acme", "a", {})
    assert store.delete_tenant("acme") is True
    assert not (tmp_path / "tenants" / "acme.db").exists()
    assert store.delete_tenant("acme") is False


def test_unknown_tenant_has_no_items(store: CellStore) -> None:
    assert store.list_items("nobody") == []


def test_rejects_unsafe_tenant_ids_for_database_paths(store: CellStore) -> None:
    with pytest.raises(ValueError, match="tenant id"):
        store.provision_tenant("../escape", isolation="isolated")


def test_old_cell_databases_are_migrated_to_pooled(tmp_path: Path) -> None:
    db = tmp_path / "cell.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "provisioned_at TEXT NOT NULL);"
            "INSERT INTO tenants VALUES ('acme', 'active', '2026-11-01T00:00:00+00:00');"
        )
    store = CellStore(str(db))
    tenant = store.get_tenant("acme")
    assert tenant is not None and tenant["isolation"] == "pooled"
    CellStore(str(db))  # migrating twice is harmless
