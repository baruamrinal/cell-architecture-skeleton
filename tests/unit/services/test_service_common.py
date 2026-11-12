from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cellarch.services.common.idempotency import (
    IdempotencyConflictError,
    IdempotencyStore,
    request_fingerprint,
)
from cellarch.services.common.migrations import Migration, apply_migrations
from cellarch.services.common.pagination import decode_cursor, encode_cursor
from cellarch.services.common.settings import ServiceSettings


class TestMigrations:
    def migrations(self) -> list[Migration]:
        return [
            Migration(1, "create things", "CREATE TABLE demo_things (id TEXT PRIMARY KEY)"),
            Migration(2, "add name", "ALTER TABLE demo_things ADD COLUMN name TEXT"),
        ]

    def test_applies_in_order_and_only_once(self, tmp_path: Path) -> None:
        db = str(tmp_path / "t.db")
        assert apply_migrations(db, "demo", self.migrations()) == [1, 2]
        assert apply_migrations(db, "demo", self.migrations()) == []
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT version, name FROM demo_schema_migrations").fetchall()
            columns = {r[1] for r in conn.execute("PRAGMA table_info(demo_things)")}
        assert rows == [(1, "create things"), (2, "add name")]
        assert columns == {"id", "name"}

    def test_new_migrations_are_applied_later(self, tmp_path: Path) -> None:
        db = str(tmp_path / "t.db")
        apply_migrations(db, "demo", self.migrations()[:1])
        assert apply_migrations(db, "demo", self.migrations()) == [2]

    def test_a_failed_migration_is_not_recorded(self, tmp_path: Path) -> None:
        db = str(tmp_path / "t.db")
        broken = [*self.migrations()[:1], Migration(2, "broken", "NOT SQL")]
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(db, "demo", broken)
        assert apply_migrations(db, "demo", self.migrations()) == [2]

    @pytest.mark.parametrize(
        "versions", [[2, 1], [1, 1], [0, 1]], ids=["unordered", "duplicate", "non-positive"]
    )
    def test_versions_must_increase_from_one(self, tmp_path: Path, versions: list[int]) -> None:
        migrations = [Migration(v, f"m{v}", "SELECT 1") for v in versions]
        with pytest.raises(ValueError, match="versions"):
            apply_migrations(str(tmp_path / "t.db"), "demo", migrations)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class TestIdempotency:
    def store(self, tmp_path: Path, clock: FakeClock | None = None) -> IdempotencyStore:
        return IdempotencyStore(
            str(tmp_path / "t.db"), "demo", ttl_seconds=60, clock=clock or FakeClock()
        )

    def test_saves_and_replays(self, tmp_path: Path) -> None:
        store = self.store(tmp_path)
        fingerprint = request_fingerprint({"a": 1})
        assert store.lookup("k1", fingerprint) is None
        store.save("k1", fingerprint, 201, {"id": "x"})
        assert store.lookup("k1", fingerprint) == (201, {"id": "x"})

    def test_same_key_different_request_conflicts(self, tmp_path: Path) -> None:
        store = self.store(tmp_path)
        store.save("k1", request_fingerprint({"a": 1}), 201, {})
        with pytest.raises(IdempotencyConflictError):
            store.lookup("k1", request_fingerprint({"a": 2}))

    def test_keys_expire(self, tmp_path: Path) -> None:
        clock = FakeClock()
        store = self.store(tmp_path, clock)
        fingerprint = request_fingerprint({"a": 1})
        store.save("k1", fingerprint, 201, {})
        clock.now += 61
        assert store.lookup("k1", fingerprint) is None
        store.save("k1", request_fingerprint({"a": 2}), 201, {"new": True})  # key reusable
        assert store.lookup("k1", request_fingerprint({"a": 2})) == (201, {"new": True})

    def test_fingerprint_ignores_key_order(self) -> None:
        assert request_fingerprint({"a": 1, "b": 2}) == request_fingerprint({"b": 2, "a": 1})


class TestCursors:
    def test_round_trip(self) -> None:
        assert decode_cursor(encode_cursor(42)) == 42

    @pytest.mark.parametrize("cursor", ["", "!!!", "e30", encode_cursor(1)[:-2] + "zz"])
    def test_invalid_cursors(self, cursor: str) -> None:
        with pytest.raises(ValueError, match="cursor"):
            decode_cursor(cursor)


class TestSettings:
    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_ID", "acme")
        monkeypatch.setenv("STACK_TOKEN", "s")
        monkeypatch.setenv("TENANT_DB_PATH", "/data/acme.db")
        assert ServiceSettings.from_env() == ServiceSettings("acme", "s", "/data/acme.db")

    def test_tenant_id_is_validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_ID", "Not Valid")
        monkeypatch.setenv("STACK_TOKEN", "s")
        with pytest.raises(ValueError, match="TENANT_ID"):
            ServiceSettings.from_env()
