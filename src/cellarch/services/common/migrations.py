"""Versioned, recorded schema migrations per service (enterprise principle: operable).

Each service owns a namespace in the tenant's database (``<namespace>_*`` tables locally,
a schema on Postgres) and records applied versions in ``<namespace>_schema_migrations``.
Each migration runs in its own transaction: it is applied completely or not at all.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cellarch.common.db import connect, utcnow

_NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,40}")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str  # one or more statements separated by ";"


def apply_migrations(db_path: str, namespace: str, migrations: Sequence[Migration]) -> list[int]:
    """Apply the migrations not applied yet; returns the versions applied now."""
    if not _NAMESPACE.fullmatch(namespace):
        raise ValueError(f"invalid migration namespace {namespace!r}")
    versions = [m.version for m in migrations]
    if versions != sorted(set(versions)) or (versions and versions[0] < 1):
        raise ValueError("migration versions must be unique, increasing and start at 1 or more")

    table = f"{namespace}_schema_migrations"
    with connect(db_path) as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        applied = {row[0] for row in conn.execute(f"SELECT version FROM {table}")}  # noqa: S608

    done: list[int] = []
    for migration in migrations:
        if migration.version in applied:
            continue
        with connect(db_path) as conn:
            conn.execute("BEGIN")
            for statement in filter(None, (s.strip() for s in migration.sql.split(";"))):
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {table} (version, name, applied_at) VALUES (?, ?, ?)",  # noqa: S608
                (migration.version, migration.name, utcnow()),
            )
        done.append(migration.version)
    return done
