"""Versioned, recorded schema migrations per service (enterprise principle: operable).

Each service owns a namespace in the tenant's database (its own schema on Postgres,
``<namespace>_*`` tables in the SQLite stand-in) and records applied versions in
``<namespace>_schema_migrations``. Each migration runs in its own transaction, so it is
applied completely or not at all. Instances starting together take a lock per namespace,
so every migration is applied exactly once.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cellarch.common.db import utcnow
from cellarch.common.sql import Database

_NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,40}")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str  # one or more statements separated by ";" (may use {seq_pk}, see common.sql)


def apply_migrations(db: Database, namespace: str, migrations: Sequence[Migration]) -> list[int]:
    """Apply the migrations not applied yet; returns the versions applied now."""
    if not _NAMESPACE.fullmatch(namespace):
        raise ValueError(f"invalid migration namespace {namespace!r}")
    versions = [m.version for m in migrations]
    if versions != sorted(set(versions)) or (versions and versions[0] < 1):
        raise ValueError("migration versions must be unique, increasing and start at 1 or more")

    table = f"{namespace}_schema_migrations"
    done: list[int] = []
    for migration in migrations:
        with db.connect(lock=table) as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} "
                "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            applied = conn.execute(
                f"SELECT 1 FROM {table} WHERE version = ?",  # noqa: S608
                (migration.version,),
            ).fetchone()
            if applied:
                continue
            for statement in filter(None, (s.strip() for s in db.ddl(migration.sql).split(";"))):
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {table} (version, name, applied_at) VALUES (?, ?, ?)",  # noqa: S608
                (migration.version, migration.name, utcnow()),
            )
        done.append(migration.version)
    return done
