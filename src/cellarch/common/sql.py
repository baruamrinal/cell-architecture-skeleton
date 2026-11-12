"""One storage interface over SQLite and PostgreSQL (enterprise principle: production equivalent).

Tenant services reach their database only through ``Database``, chosen by
``DATABASE_URL``:

* ``postgresql://…``  - the cell's database server (Aurora PostgreSQL on AWS);
* ``sqlite:///<path>`` - the in-process stand-in for tests and local runs.

Code written against it is portable:
* SQL uses ``?`` placeholders (translated for Postgres), never ``?`` inside literals;
* rows are plain dicts;
* ``with db.connect() as conn:`` is one transaction, committed on success and rolled
  back on any error; ``lock=`` serialises transactions that hold the same name
  (a Postgres advisory lock; an immediate write lock on SQLite);
* constraint violations raise ``IntegrityError``, every other driver failure
  ``DatabaseError``, so callers never import a driver;
* DDL may use ``{seq_pk}``: an increasing key that is never reused.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

Row = dict[str, Any]
SQLITE_PREFIX = "sqlite:///"


class DatabaseError(Exception):
    """The database could not be reached or rejected a statement."""


class IntegrityError(DatabaseError):
    """A constraint was violated (for example a unique key)."""


class Result(Protocol):
    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> Row | None: ...

    def fetchall(self) -> list[Row]: ...


class Connection(Protocol):
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result: ...


class Database(ABC):
    dialect: str
    _seq_pk: str

    @abstractmethod
    def connect(self, *, lock: str | None = None) -> AbstractContextManager[Connection]:
        """One transaction: committed when the block succeeds, rolled back otherwise."""

    def ddl(self, sql: str) -> str:
        return sql.replace("{seq_pk}", self._seq_pk)

    def ping(self) -> None:
        """Raises ``DatabaseError`` if the database can't answer a query."""
        with self.connect() as conn:
            conn.execute("SELECT 1")


# --- SQLite -----------------------------------------------------------------------------


class _SqliteResult:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> Row | None:
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[Row]:
        return [dict(row) for row in self._cursor.fetchall()]


class _SqliteConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        try:
            return _SqliteResult(self._conn.execute(sql, tuple(params)))
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc


class SqliteDatabase(Database):
    dialect = "sqlite"
    _seq_pk = "INTEGER PRIMARY KEY AUTOINCREMENT"

    def __init__(self, path: str) -> None:
        self.path = path

    def __repr__(self) -> str:
        return f"SqliteDatabase({self.path!r})"

    @contextmanager
    def connect(self, *, lock: str | None = None) -> Iterator[Connection]:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            raw = sqlite3.connect(self.path, timeout=10, autocommit=True)
            raw.row_factory = sqlite3.Row
            # SQLite has one write lock per database: take it up front when asked to.
            raw.execute("BEGIN IMMEDIATE" if lock else "BEGIN")
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseError(f"cannot open the database: {exc}") from exc
        try:
            yield _SqliteConnection(raw)
            raw.execute("COMMIT")
        except BaseException:
            raw.execute("ROLLBACK")
            raise
        finally:
            raw.close()


# --- PostgreSQL -------------------------------------------------------------------------


class DriverConnection(Protocol):
    """The part of a psycopg connection the adapter uses."""

    def execute(self, query: Any, params: Any = ...) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


def _psycopg_connect(url: str) -> DriverConnection:
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)


class _PostgresConnection:
    def __init__(self, conn: DriverConnection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        query = sql.replace("%", "%%").replace("?", "%s")
        try:
            result: Result = self._conn.execute(query, tuple(params))
        except psycopg.errors.IntegrityError as exc:
            raise IntegrityError(type(exc).__name__) from exc
        except psycopg.Error as exc:
            # The driver's message may quote row values: keep it out of our message.
            raise DatabaseError(type(exc).__name__) from exc
        return result


class PostgresDatabase(Database):
    dialect = "postgresql"
    _seq_pk = "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"

    def __init__(
        self, url: str, *, connect: Callable[[str], DriverConnection] = _psycopg_connect
    ) -> None:
        self._url = url
        self._connect = connect

    def __repr__(self) -> str:
        parts = urlsplit(self._url)
        return f"PostgresDatabase(host={parts.hostname!r}, database={parts.path[1:]!r})"

    @contextmanager
    def connect(self, *, lock: str | None = None) -> Iterator[Connection]:
        try:
            raw = self._connect(self._url)
        except psycopg.Error as exc:
            raise DatabaseError("cannot connect to the database") from exc
        conn = _PostgresConnection(raw)
        try:
            if lock:  # held until this transaction ends
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (lock,))
            yield conn
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()


# --- configuration ----------------------------------------------------------------------


def sqlite_url(path: Path | str) -> str:
    return SQLITE_PREFIX + Path(path).as_posix()


def open_database(url: str) -> Database:
    """The database a ``DATABASE_URL`` points at."""
    if url.startswith(SQLITE_PREFIX) and len(url) > len(SQLITE_PREFIX):
        return SqliteDatabase(url[len(SQLITE_PREFIX) :])
    if urlsplit(url).scheme in ("postgresql", "postgres"):
        return PostgresDatabase(url)
    # Never echo the URL: it may hold a password.
    raise ValueError("DATABASE_URL must be sqlite:///<path> or postgresql://<user>@<host>/<db>")
