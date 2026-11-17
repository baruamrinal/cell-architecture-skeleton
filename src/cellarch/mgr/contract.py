"""Every ``/v1`` call mgr makes, checked against the tenant services' OpenAPI.

mgr and the tenant services release independently, so neither may rely on the other's
exact version. This contract pins what mgr uses: each call's method, path, query
parameters and the response fields it reads. ``unmet()`` checks the services still
offer all of it; their CI runs the same check, so a change that would break mgr fails
before it ships.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cellarch.common.sql import sqlite_url


@dataclass(frozen=True)
class Call:
    service: str  # "cell-gateway", "user-account" or "order-processing"
    method: str
    path: str  # OpenAPI path template
    query: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset()  # response fields mgr uses


USER = frozenset({"id", "email", "name", "created_at"})
ORDER = frozenset({"id", "user_id", "status", "lines", "total_cents", "created_at", "updated_at"})
PAGE = frozenset({"items", "next_cursor"})
SERVICES = ("cell-gateway", "user-account", "order-processing")

CALLS = (
    Call("cell-gateway", "get", "/v1/tenant", reads=frozenset({"tenant_id", "status", "cell"})),
    Call("user-account", "get", "/v1/users", frozenset({"limit", "cursor"}), PAGE),
    Call("user-account", "get", "/v1/users/{user_id}", reads=USER),
    Call("user-account", "post", "/v1/users", reads=USER),
    Call("user-account", "delete", "/v1/users/{user_id}"),
    Call("order-processing", "get", "/v1/orders", frozenset({"limit", "cursor", "user_id"}), PAGE),
    Call("order-processing", "get", "/v1/orders/{order_id}", reads=ORDER),
    Call("order-processing", "post", "/v1/orders", reads=ORDER),
    Call("order-processing", "post", "/v1/orders/{order_id}/cancel", reads=ORDER),
)


class _NoSecrets:
    """The gateway's secrets, never used: only its OpenAPI is read."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - never called
        raise AssertionError(f"contract check used secrets.{name}")


def _openapi(service: str, directory: Path) -> dict[str, Any]:
    from cellarch.data_plane.cell.app import CellSettings
    from cellarch.data_plane.cell.app import create_app as cell_gateway
    from cellarch.services.common.settings import ServiceSettings
    from cellarch.services.order_processing.app import OrderSettings
    from cellarch.services.order_processing.app import create_app as order_processing
    from cellarch.services.user_account.app import create_app as user_account

    database = sqlite_url(directory / f"{service}.db")
    if service == "cell-gateway":
        settings = CellSettings(
            "contract",
            str(directory / "gateway.db"),
            "contract",
            tenant_db_admin_url=sqlite_url(directory / "tenants"),
        )
        app = cell_gateway(settings, secrets=_NoSecrets())
    elif service == "user-account":
        app = user_account(ServiceSettings("contract", "contract", database))
    else:
        app = order_processing(
            OrderSettings("contract", "contract", database, user_account_url="http://unused")
        )
    spec: dict[str, Any] = app.openapi()
    return spec


def _fields(spec: dict[str, Any], schema: dict[str, Any]) -> set[str]:
    ref = schema.get("$ref")
    if ref:
        schema = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    return set(schema.get("properties", {}))


def unmet() -> list[str]:
    """What mgr relies on that the tenant services no longer offer (empty: all good)."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        specs = {s: _openapi(s, Path(directory)) for s in SERVICES}
    for call in CALLS:
        spec = specs[call.service]
        operation = spec.get("paths", {}).get(call.path, {}).get(call.method)
        name = f"{call.method.upper()} {call.path}"
        if operation is None:
            problems.append(f"{name}: not offered by {call.service}")
            continue
        offered = {p["name"] for p in operation.get("parameters", []) if p.get("in") == "query"}
        problems += [f"{name}: no query parameter {q}" for q in sorted(call.query - offered)]
        if call.reads:
            responses = operation.get("responses", {})
            ok = responses.get("200") or responses.get("201") or {}
            schema = ok.get("content", {}).get("application/json", {}).get("schema", {})
            problems += [
                f"{name}: response has no field {f}"
                for f in sorted(call.reads - _fields(spec, schema))
            ]
    return problems
