"""Exporting and importing a tenant's data for a move (docs/plan.md §5).

Runs on the SQLite stand-in; tests/acceptance (A64) runs the same round trip on Postgres.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cellarch.common.sql import open_database
from cellarch.data_plane.cell.tenant_data import (
    FORMAT,
    TenantDataError,
    export_tenant,
    import_tenant,
)
from cellarch.data_plane.cell.tenant_resources import SqliteTenantDatabases
from cellarch.services.common.settings import ServiceSettings
from cellarch.services.order_processing.app import OrderSettings
from cellarch.services.order_processing.app import create_app as create_order_processing
from cellarch.services.user_account.app import create_app as create_user_account


def start_stack(databases: SqliteTenantDatabases, tenant_id: str = "acme") -> str:
    """Create the tenant's database and let its services create their tables."""
    urls = databases.create(tenant_id)
    create_user_account(ServiceSettings(tenant_id, "t", urls["user-account"]))
    create_order_processing(
        OrderSettings(tenant_id, "t", urls["order-processing"], user_account_url="http://ua")
    )
    return urls["user-account"]


def add_users(url: str, count: int, tenant_id: str = "acme") -> None:
    with open_database(url).connect() as conn:
        for i in range(count):
            conn.execute(
                "INSERT INTO user_account_users (id, tenant_id, email, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"usr_{i}", tenant_id, f"u{i}@example.test", f"U{i}", "2026-01-01T00:00:00Z"),
            )


def records(lines: list[bytes]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in lines]


@pytest.fixture
def source(tmp_path: Path) -> SqliteTenantDatabases:
    return SqliteTenantDatabases(str(tmp_path / "source"))


@pytest.fixture
def target(tmp_path: Path) -> SqliteTenantDatabases:
    return SqliteTenantDatabases(str(tmp_path / "target"))


@pytest.fixture
def exported(source: SqliteTenantDatabases) -> list[bytes]:
    add_users(start_stack(source), 3)
    return list(export_tenant(source, "acme"))


class TestExport:
    def test_header_rows_per_table_then_counts(self, exported: list[bytes]) -> None:
        header, *body, end = records(exported)
        assert header == {
            "kind": "header",
            "format": FORMAT,
            "tenant_id": "acme",
            "services": {
                "user-account": {"schema_version": 1},
                "order-processing": {"schema_version": 1},
            },
        }
        [rows] = body
        assert (rows["kind"], rows["service"], rows["table"]) == (
            "rows",
            "user-account",
            "user_account_users",
        )
        assert [r["id"] for r in rows["rows"]] == ["usr_0", "usr_1", "usr_2"]
        assert {r["seq"] for r in rows["rows"]} == {1, 2, 3}  # keys travel too
        assert end == {
            "kind": "end",
            "counts": {
                "user_account_users": 3,
                "user_account_idempotency_keys": 0,
                "order_processing_orders": 0,
                "order_processing_idempotency_keys": 0,
            },
        }
        assert all(line.endswith(b"\n") for line in exported)

    def test_large_tables_go_in_batches(self, source: SqliteTenantDatabases) -> None:
        add_users(start_stack(source), 5)
        body = records(list(export_tenant(source, "acme", batch_rows=2)))[1:-1]
        assert [len(r["rows"]) for r in body] == [2, 2, 1]

    def test_values_json_cannot_carry_are_refused(self, source: SqliteTenantDatabases) -> None:
        url = start_stack(source)
        with open_database(url).connect() as conn:
            conn.execute(
                "INSERT INTO user_account_users (id, tenant_id, email, name, created_at) "
                "VALUES ('usr_b', 'acme', 'b@example.test', ?, 'now')",
                (b"\x00binary",),
            )
        with pytest.raises(TenantDataError, match="cannot be exported") as raised:
            list(export_tenant(source, "acme"))
        assert raised.value.code == "invalid_export"


class TestImport:
    def test_round_trip_keeps_rows_and_keys(
        self, exported: list[bytes], target: SqliteTenantDatabases
    ) -> None:
        url = start_stack(target)
        counts = import_tenant(target, "acme", exported)
        assert counts["user_account_users"] == 3
        with open_database(url).connect() as conn:
            rows = conn.execute("SELECT id, seq FROM user_account_users ORDER BY seq").fetchall()
            conn.execute(
                "INSERT INTO user_account_users (id, tenant_id, email, name, created_at) "
                "VALUES ('usr_new', 'acme', 'new@example.test', 'N', 'now')"
            )
            new = conn.execute("SELECT seq FROM user_account_users WHERE id = 'usr_new'")
            assert new.fetchone() == {"seq": 4}
        assert [(r["id"], r["seq"]) for r in rows] == [("usr_0", 1), ("usr_1", 2), ("usr_2", 3)]

    def test_refuses_a_target_without_its_stack(
        self, exported: list[bytes], target: SqliteTenantDatabases
    ) -> None:
        target.create("acme")  # database only: no tables yet
        self.assert_refused(target, exported, "tenant_stack_not_ready", 409)

    def test_refuses_a_target_that_has_data(
        self, exported: list[bytes], target: SqliteTenantDatabases
    ) -> None:
        add_users(start_stack(target), 1)
        self.assert_refused(target, exported, "tenant_data_not_empty", 409)

    def test_refuses_another_schema_version(
        self, exported: list[bytes], target: SqliteTenantDatabases
    ) -> None:
        start_stack(target)
        header = json.loads(exported[0])
        header["services"]["order-processing"]["schema_version"] = 2
        tampered = [json.dumps(header).encode(), *exported[1:]]
        self.assert_refused(target, tampered, "schema_version_mismatch", 409)

    @pytest.mark.parametrize(
        "damage",
        [
            "truncated",
            "count-mismatch",
            "other-tenant",
            "unknown-format",
            "not-json",
            "unknown-table",
            "migration-records",
            "unknown-service",
            "empty",
            "rows-before-header",
        ],
    )
    def test_refuses_a_damaged_export(
        self, exported: list[bytes], target: SqliteTenantDatabases, damage: str
    ) -> None:
        start_stack(target)
        self.assert_refused(target, list(_damaged(exported, damage)), "invalid_export", 422)
        with open_database(target.create("acme")["user-account"]).connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM user_account_users").fetchone() == {
                "n": 0
            }

    def test_rows_the_target_rejects_fail_the_import(
        self, exported: list[bytes], target: SqliteTenantDatabases
    ) -> None:
        start_stack(target)
        lines = records(exported)
        lines[1]["rows"].append(dict(lines[1]["rows"][0]))  # duplicate key
        lines[-1]["counts"]["user_account_users"] += 1
        self.assert_refused(
            target, [json.dumps(r).encode() for r in lines], "tenant_import_failed", 422
        )

    @staticmethod
    def assert_refused(
        target: SqliteTenantDatabases, lines: list[bytes], code: str, status: int
    ) -> None:
        with pytest.raises(TenantDataError) as raised:
            import_tenant(target, "acme", lines)
        assert (raised.value.code, raised.value.status) == (code, status)


def _damaged(exported: list[bytes], damage: str) -> Iterator[bytes]:
    lines = records(exported)
    if damage == "truncated":
        lines = lines[:-1]
    elif damage == "count-mismatch":
        lines[-1]["counts"]["user_account_users"] = 5
    elif damage == "other-tenant":
        lines[0]["tenant_id"] = "globex"
    elif damage == "unknown-format":
        lines[0]["format"] = FORMAT + 1
    elif damage == "not-json":
        yield exported[0]
        yield b"{not json"
        return
    elif damage == "unknown-table":
        lines[1]["table"] = "user_account_secrets"
        lines[-1]["counts"] = {"user_account_secrets": 3}
    elif damage == "migration-records":
        lines[1]["table"] = "user_account_schema_migrations"
        lines[-1]["counts"] = {"user_account_schema_migrations": 3}
    elif damage == "unknown-service":
        lines[1]["service"] = "billing"
    elif damage == "empty":
        return
    else:
        lines = [lines[1], lines[0], *lines[2:]]
    for line in lines:
        yield json.dumps(line).encode()


class TestLooseInput:
    def test_blank_lines_are_ignored(
        self, exported: list[bytes], target: SqliteTenantDatabases
    ) -> None:
        start_stack(target)
        spaced = [part for line in exported for part in (line, b"\n")]
        assert import_tenant(target, "acme", spaced)["user_account_users"] == 3

    @pytest.mark.parametrize(
        "lines",
        [
            [b"[1, 2]"],
            [
                json.dumps(
                    {"kind": "header", "format": FORMAT, "tenant_id": "acme", "services": []}
                ).encode()
            ],
        ],
        ids=["not-an-object", "services-not-listed"],
    )
    def test_malformed_lines(self, target: SqliteTenantDatabases, lines: list[bytes]) -> None:
        start_stack(target)
        TestImport.assert_refused(target, lines, "invalid_export", 422)
