"""The storage interface behaves the same on SQLite and PostgreSQL.

Contract tests run on every available backend (see tests/conftest.py). The Postgres
driver boundary is also tested with a fake connection, so the adapter's own logic is
covered without a server.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cellarch.common.sql import (
    Database,
    DatabaseError,
    IntegrityError,
    PostgresDatabase,
    SqliteDatabase,
    open_database,
    sqlite_url,
)

TABLE = "CREATE TABLE things (id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, seq {seq_pk})"


class TestContract:
    def test_rows_are_dicts_and_placeholders_are_portable(self, database_url: str) -> None:
        db = open_database(database_url)
        with db.connect() as conn:
            conn.execute(db.ddl(TABLE))
            conn.execute("INSERT INTO things (id, name) VALUES (?, ?)", ("a", "50% off"))
            row = conn.execute("SELECT id, name FROM things WHERE id = ?", ("a",)).fetchone()
            missing = conn.execute("SELECT id FROM things WHERE id = ?", ("x",)).fetchone()
        assert row == {"id": "a", "name": "50% off"}
        assert missing is None

    def test_sequence_keys_increase_and_are_never_reused(self, database_url: str) -> None:
        db = open_database(database_url)
        with db.connect() as conn:
            conn.execute(db.ddl(TABLE))
            for item in ("a", "b"):
                conn.execute("INSERT INTO things (id, name) VALUES (?, ?)", (item, item))
            conn.execute("DELETE FROM things WHERE id = ?", ("b",))
            conn.execute("INSERT INTO things (id, name) VALUES (?, ?)", ("c", "c"))
            rows = conn.execute("SELECT id, seq FROM things ORDER BY seq").fetchall()
        assert [r["id"] for r in rows] == ["a", "c"]
        assert rows[0]["seq"] < 2 < rows[1]["seq"]  # "b" had 2; it is not handed out again

    def test_a_transaction_commits_or_rolls_back_completely(self, database_url: str) -> None:
        db = open_database(database_url)
        with db.connect() as conn:
            conn.execute(db.ddl(TABLE))
        with pytest.raises(RuntimeError), db.connect() as conn:
            conn.execute("INSERT INTO things (id, name) VALUES (?, ?)", ("a", "a"))
            raise RuntimeError("boom")
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM things").fetchone() == {"n": 0}
            changed = conn.execute("DELETE FROM things WHERE id = ?", ("none",))
            assert changed.rowcount == 0

    def test_constraint_violations_raise_integrity_error(self, database_url: str) -> None:
        db = open_database(database_url)
        with db.connect() as conn:
            conn.execute(db.ddl(TABLE))
            conn.execute("INSERT INTO things (id, name) VALUES (?, ?)", ("a", "a"))
        with pytest.raises(IntegrityError), db.connect() as conn:
            conn.execute("INSERT INTO things (id, name) VALUES (?, ?)", ("a", "again"))

    def test_bad_sql_raises_database_error(self, database_url: str) -> None:
        db = open_database(database_url)
        with pytest.raises(DatabaseError) as raised, db.connect() as conn:
            conn.execute("NOT SQL")
        assert not isinstance(raised.value, IntegrityError)

    def test_ping(self, database_url: str) -> None:
        open_database(database_url).ping()

    def test_a_lock_serialises_transactions(self, database_url: str) -> None:
        """Two writers holding the same lock never interleave (used by migrations)."""
        db = open_database(database_url)
        with db.connect() as conn:
            conn.execute("CREATE TABLE counter (n INTEGER NOT NULL)")
            conn.execute("INSERT INTO counter (n) VALUES (0)")

        def increment() -> None:
            for _ in range(5):
                with db.connect(lock="counter") as conn:
                    row = conn.execute("SELECT n FROM counter").fetchone()
                    n = row["n"] if row else 0
                    conn.execute("UPDATE counter SET n = ?", (n + 1,))

        threads = [threading.Thread(target=increment) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with db.connect() as conn:
            assert conn.execute("SELECT n FROM counter").fetchone() == {"n": 20}


class TestOpenDatabase:
    def test_sqlite_urls(self, tmp_path: Path) -> None:
        db = open_database(sqlite_url(tmp_path / "sub" / "t.db"))
        assert isinstance(db, SqliteDatabase)
        assert db.dialect == "sqlite"
        db.ping()  # creates the folder and the file
        assert (tmp_path / "sub" / "t.db").is_file()

    def test_relative_sqlite_url(self) -> None:
        db = open_database("sqlite:///./data/t.db")
        assert isinstance(db, SqliteDatabase)
        assert db.path == "./data/t.db"
        assert repr(db) == "SqliteDatabase('./data/t.db')"

    @pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
    def test_postgres_urls(self, scheme: str) -> None:
        db = open_database(f"{scheme}://u:secret@db.example:5432/tenant_acme")
        assert isinstance(db, PostgresDatabase)
        assert db.dialect == "postgresql"

    @pytest.mark.parametrize("url", ["", "mysql://x/y", "sqlite://", "/data/t.db"])
    def test_unsupported_urls_are_rejected_without_echoing_them(self, url: str) -> None:
        with pytest.raises(ValueError, match="DATABASE_URL") as raised:
            open_database(url)
        assert "secret" not in str(raised.value)

    def test_password_is_not_in_the_representation(self) -> None:
        db = open_database("postgresql://u:secret@db.example/tenant_acme")
        assert "secret" not in repr(db)


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class FakePgConnection:
    """Stands in for a psycopg connection at the driver boundary."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_with = fail_with
        self.committed = self.rolled_back = self.closed = False

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        self.statements.append((query, tuple(params)))
        if self.fail_with is not None:
            raise self.fail_with
        return FakeCursor([{"one": 1}], rowcount=1)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class TestPostgresAdapter:
    def database(self, conn: FakePgConnection) -> PostgresDatabase:
        return PostgresDatabase("postgresql://u:pw@h/db", connect=lambda url: conn)

    def test_translates_placeholders_and_escapes_percent(self) -> None:
        conn = FakePgConnection()
        with self.database(conn).connect() as session:
            row = session.execute("SELECT * FROM t WHERE a = ? AND b LIKE '5%'", (1,)).fetchone()
        assert conn.statements == [("SELECT * FROM t WHERE a = %s AND b LIKE '5%%'", (1,))]
        assert row == {"one": 1}
        assert conn.committed and conn.closed and not conn.rolled_back

    def test_takes_a_transaction_scoped_advisory_lock(self) -> None:
        conn = FakePgConnection()
        with self.database(conn).connect(lock="demo_schema_migrations"):
            pass
        assert conn.statements == [
            ("SELECT pg_advisory_xact_lock(hashtext(%s))", ("demo_schema_migrations",))
        ]

    def test_sequence_keys_use_identity_columns(self) -> None:
        db = self.database(FakePgConnection())
        assert db.ddl("seq {seq_pk}") == "seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (psycopg.errors.UniqueViolation("duplicate key"), IntegrityError),
            (psycopg.errors.SyntaxError("syntax error"), DatabaseError),
        ],
    )
    def test_driver_errors_are_translated_and_rolled_back(
        self, error: Exception, expected: type[Exception]
    ) -> None:
        conn = FakePgConnection(fail_with=error)
        with pytest.raises(expected), self.database(conn).connect() as session:
            session.execute("INSERT INTO t VALUES (?)", (1,))
        assert conn.rolled_back and conn.closed and not conn.committed

    def test_connection_failures_raise_database_error(self) -> None:
        def refuse(url: str) -> FakePgConnection:
            raise psycopg.OperationalError("connection refused")

        db = PostgresDatabase("postgresql://u:pw@h/db", connect=refuse)
        with pytest.raises(DatabaseError, match="cannot connect"):
            db.ping()


def test_sqlite_connection_failures_raise_database_error(tmp_path: Path) -> None:
    (tmp_path / "a-folder.db").mkdir()  # a directory is not a database file
    db: Database = SqliteDatabase(str(tmp_path / "a-folder.db"))
    with pytest.raises(DatabaseError):
        db.ping()
