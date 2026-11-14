"""A tenant's data as a logical export, for moving it to another cell (docs/plan.md §5).

The export is JSON lines, written and read through the storage interface, so the
SQLite stand-in and Postgres behave the same:

    {"kind": "header", "format": 1, "tenant_id": "acme",
     "services": {"user-account": {"schema_version": 1}, ...}}
    {"kind": "rows", "service": "user-account", "table": "user_account_users", "rows": [...]}
    ...
    {"kind": "end", "counts": {"user_account_users": 3, ...}}

* Every table of each service is exported, keys included, except its migration
  records: the target's stack applies its own migrations, and the import refuses a
  different schema version.
* Each service's tables are read and written as that service's login (on Postgres), so
  a copy can't reach another service's schema or another tenant's database.
* The import checks the whole export first (complete, for this tenant, counts match),
  refuses a target with data, then writes one transaction per service. A failed import
  is cleaned up by the move's rollback, which drops the target database.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from cellarch.common.sql import Connection, Database, DatabaseError, identifier
from cellarch.data_plane.cell.tenant_resources import SERVICES, TenantDatabases, schema_name

FORMAT = 1
BATCH_ROWS = 500
_JSON_VALUES = (str, int, float, bool, type(None))


class TenantDataError(Exception):
    """An export or import was refused; ``code`` and ``status`` go into the API error."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _invalid(message: str) -> TenantDataError:
    return TenantDataError("invalid_export", message, 422)


def _migrations_table(service: str) -> str:
    return f"{schema_name(service)}_schema_migrations"


def _data_tables(db: Database, conn: Connection, service: str) -> list[str]:
    prefix = f"{schema_name(service)}_"
    return [t for t in db.tables(conn) if t.startswith(prefix) and t != _migrations_table(service)]


def _schema_version(db: Database, conn: Connection, service: str) -> int | None:
    """The newest migration applied, or ``None`` if the service never started here."""
    table = _migrations_table(service)
    if table not in db.tables(conn):
        return None
    row = conn.execute(f"SELECT MAX(version) AS v FROM {table}").fetchone()  # noqa: S608
    return int(row["v"]) if row and row["v"] is not None else 0


def _line(record: dict[str, Any]) -> bytes:
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


# --- export ------------------------------------------------------------------------------


def export_tenant(
    databases: TenantDatabases, tenant_id: str, *, batch_rows: int = BATCH_ROWS
) -> Iterator[bytes]:
    """The tenant's data as JSON lines. The caller makes sure the tenant is frozen."""
    services = {s: databases.service_database(tenant_id, s) for s in SERVICES}
    versions: dict[str, dict[str, int | None]] = {}
    for service, db in services.items():
        with db.connect() as conn:
            versions[service] = {"schema_version": _schema_version(db, conn, service)}
    yield _line({"kind": "header", "format": FORMAT, "tenant_id": tenant_id, "services": versions})

    counts: dict[str, int] = {}
    for service, db in services.items():
        with db.connect() as conn:
            for table in _data_tables(db, conn, service):
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
                counts[table] = len(rows)
                for start in range(0, len(rows), batch_rows):
                    batch = rows[start : start + batch_rows]
                    for row in batch:
                        if not all(isinstance(v, _JSON_VALUES) for v in row.values()):
                            raise _invalid(f"a row of {table} cannot be exported as JSON")
                    yield _line({"kind": "rows", "service": service, "table": table, "rows": batch})
    yield _line({"kind": "end", "counts": counts})


# --- import ------------------------------------------------------------------------------


def _parse(lines: Iterable[bytes | str]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise _invalid("the export is not valid JSON lines") from exc
        if not isinstance(record, dict):
            raise _invalid("every line of the export must be an object")
        parsed.append(record)
    return parsed


def _check_header(record: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    if record.get("kind") != "header":
        raise _invalid("the export must start with its header")
    if record.get("format") != FORMAT:
        raise _invalid(f"unsupported export format {record.get('format')!r}")
    if record.get("tenant_id") != tenant_id:
        raise _invalid("the export belongs to another tenant")
    services = record.get("services")
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise _invalid("the export header must list every service")
    return services


def _group_rows(
    body: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, int]]:
    """Rows per service and table, checked against the end record's counts."""
    if not body or body[-1].get("kind") != "end" or not isinstance(body[-1].get("counts"), dict):
        raise _invalid("the export is incomplete: it has no end record")
    expected = body[-1]["counts"]
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {s: {} for s in SERVICES}
    received: dict[str, int] = dict.fromkeys(expected, 0)
    for record in body[:-1]:
        service, table, rows = record.get("service"), record.get("table"), record.get("rows")
        if record.get("kind") != "rows" or service not in grouped or not isinstance(rows, list):
            raise _invalid("the export has a record that is not a batch of rows")
        if (
            not isinstance(table, str)
            or not table.startswith(f"{schema_name(service)}_")
            or table == _migrations_table(service)
        ):
            raise _invalid(f"{service} has no table {table!r} to import")
        grouped[service].setdefault(table, []).extend(rows)
        received[table] = received.get(table, 0) + len(rows)
    if received != expected:
        raise _invalid("the export is incomplete: row counts don't match its end record")
    return grouped, {t: n for t, n in received.items()}


def import_tenant(
    databases: TenantDatabases, tenant_id: str, lines: Iterable[bytes | str]
) -> dict[str, int]:
    """Load an export into the tenant's empty database; returns rows per table."""
    records = _parse(lines)
    if not records:
        raise _invalid("the export is empty")
    versions = _check_header(records[0], tenant_id)
    grouped, counts = _group_rows(records[1:])

    services = {s: databases.service_database(tenant_id, s) for s in SERVICES}
    for service, db in services.items():
        with db.connect() as conn:
            version = _schema_version(db, conn, service)
            if version is None:
                raise TenantDataError(
                    "tenant_stack_not_ready", f"{service} has not created its tables yet"
                )
            declared = versions[service]
            if not isinstance(declared, dict) or version != declared.get("schema_version"):
                raise TenantDataError(
                    "schema_version_mismatch",
                    f"{service} is at schema version {version} here, not the export's",
                )
            tables = _data_tables(db, conn, service)
            for table in tables:
                if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():  # noqa: S608
                    raise TenantDataError(
                        "tenant_data_not_empty", "the tenant already has data on this cell"
                    )
            unknown = sorted(set(grouped[service]) - set(tables))
            if unknown:
                raise _invalid(f"{service} has no table {unknown[0]!r} to import")

    for service, db in services.items():
        try:
            with db.connect() as conn:
                for table, rows in grouped[service].items():
                    for row in rows:
                        columns = [identifier(c) for c in row]
                        conn.execute(db.insert_keeping_keys(table, columns), list(row.values()))
                    if rows and "seq" in rows[0]:
                        db.continue_sequence(conn, table)
        except (DatabaseError, ValueError, AttributeError) as exc:
            raise TenantDataError(
                "tenant_import_failed", f"{service} rejected the imported rows", 422
            ) from exc
    return counts
