"""mgr: the web UI's GraphQL endpoint in a cell (docs/plan.md §2.4).

* Only its cell's gateway may call it (the gateway's ``mgr`` credential); the gateway
  has already checked that the cell hosts the tenant and passes it in ``X-Tenant-ID``.
* Queries come as GET, mutations as POST with the anti-CSRF header: the router and the
  gateway pause writes during a tenant move by method, so reads keep working.
* Queries are refused before they run when too deep or too costly; introspection is
  off unless enabled (it is in local development).
* Errors carry the API's error shape in ``errors[].extensions`` (``code``, ``status``,
  ``retryAfter``).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    VariableNode,
    parse,
)

from cellarch.common.config import require_env
from cellarch.common.errors import graphql_error_response
from cellarch.common.headers import INTERNAL_TOKEN_HEADER, REQUEST_ID_HEADER, TENANT_HEADER
from cellarch.common.ids import is_valid_tenant_id
from cellarch.common.logging import configure_logging
from cellarch.mgr import __version__
from cellarch.mgr.gateway import Gateway, GatewayError
from cellarch.mgr.schema import NESTED_ORDERS, PAGE_SIZE, Context, schema

CSRF_HEADER = "X-Cellarch-CSRF"
MAX_DEPTH = 8
MAX_COST = 1000
# Fields whose ``first`` argument multiplies the cost of what is selected under them.
_PAGED = {"users": PAGE_SIZE, "orders": PAGE_SIZE}
_NESTED_PAGED = {"orders": NESTED_ORDERS}


@dataclass(frozen=True)
class MgrSettings:
    cell_id: str
    gateway_url: str  # this cell's gateway
    gateway_token: str = field(repr=False)  # the credential the gateway presents to mgr
    introspection: bool = False
    max_depth: int = MAX_DEPTH
    max_cost: int = MAX_COST
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> MgrSettings:
        return cls(
            cell_id=require_env("CELL_ID"),
            gateway_url=require_env("GATEWAY_URL"),
            gateway_token=require_env("MGR_TOKEN"),
            introspection=os.environ.get("GRAPHQL_INTROSPECTION", "false").lower() == "true",
            max_depth=int(os.environ.get("GRAPHQL_MAX_DEPTH", str(MAX_DEPTH))),
            max_cost=int(os.environ.get("GRAPHQL_MAX_COST", str(MAX_COST))),
        )


class RequestRefusedError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def graphql_error(
    status: int, code: str, message: str, retry_after: int | None = None
) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return graphql_error_response(status, code, message, headers)


# --- checks before a query runs ----------------------------------------------------------


def _first(node: FieldNode, variables: Mapping[str, Any], default: int) -> int:
    for argument in node.arguments or ():
        if argument.name.value == "first":
            value = argument.value
            if isinstance(value, IntValueNode):
                return int(value.value)
            if isinstance(value, VariableNode):
                given = variables.get(value.name.value)
                return int(given) if isinstance(given, int) else default
    return default


def _measure(
    selection: SelectionSetNode | None,
    fragments: Mapping[str, FragmentDefinitionNode],
    variables: Mapping[str, Any],
    *,
    depth: int,
    multiplier: int,
    nested: bool,
    introspection: bool,
    seen: frozenset[str] = frozenset(),
) -> tuple[int, int]:
    """(deepest field level, cost) of a selection; cost counts every field instance."""
    if selection is None:
        return depth - 1, 0
    deepest, cost = depth - 1, 0
    for node in selection.selections:
        if isinstance(node, FieldNode):
            name = node.name.value
            if name in ("__schema", "__type") and not introspection:
                raise RequestRefusedError(400, "introspection_disabled", "introspection is off")
            paged = (_NESTED_PAGED if nested else _PAGED).get(name)
            child_multiplier = multiplier * _first(node, variables, paged) if paged else multiplier
            child_depth, child_cost = _measure(
                node.selection_set,
                fragments,
                variables,
                depth=depth + 1,
                multiplier=child_multiplier,
                nested=nested or paged is not None,
                introspection=introspection,
                seen=seen,
            )
            deepest, cost = max(deepest, child_depth, depth), cost + multiplier + child_cost
        elif isinstance(node, InlineFragmentNode):
            d, c = _measure(
                node.selection_set, fragments, variables, depth=depth, multiplier=multiplier,
                nested=nested, introspection=introspection, seen=seen,
            )  # fmt: skip
            deepest, cost = max(deepest, d), cost + c
        else:  # a fragment spread: the only other kind of selection
            name = cast(FragmentSpreadNode, node).name.value
            if name in seen or name not in fragments:
                raise RequestRefusedError(400, "invalid_query", f"bad fragment {name}")
            d, c = _measure(
                fragments[name].selection_set, fragments, variables, depth=depth,
                multiplier=multiplier, nested=nested, introspection=introspection,
                seen=seen | {name},
            )  # fmt: skip
            deepest, cost = max(deepest, d), cost + c
    return deepest, cost


def check(
    document: str,
    variables: Mapping[str, Any],
    operation_name: str | None,
    *,
    method: str,
    settings: MgrSettings,
) -> None:
    """Refuse a request before it runs: wrong method, too deep, too costly."""
    try:
        ast = parse(document)
    except GraphQLError as exc:
        raise RequestRefusedError(400, "invalid_query", exc.message) from exc
    operations = [d for d in ast.definitions if isinstance(d, OperationDefinitionNode)]
    fragments = {d.name.value: d for d in ast.definitions if isinstance(d, FragmentDefinitionNode)}
    chosen = [
        op
        for op in operations
        if operation_name is None or (op.name and op.name.value == operation_name)
    ]
    if len(chosen) != 1:
        raise RequestRefusedError(400, "invalid_query", "send exactly one operation")
    operation = chosen[0]
    if operation.operation is OperationType.SUBSCRIPTION:
        raise RequestRefusedError(400, "invalid_query", "subscriptions are not supported")
    if operation.operation is OperationType.MUTATION and method != "POST":
        raise RequestRefusedError(405, "mutation_not_allowed", "send mutations as POST")
    depth, cost = _measure(
        operation.selection_set, fragments, variables, depth=1, multiplier=1, nested=False,
        introspection=settings.introspection,
    )  # fmt: skip
    if depth > settings.max_depth:
        raise RequestRefusedError(
            400, "query_too_deep", f"the query is {depth} levels deep; at most {settings.max_depth}"
        )
    if cost > settings.max_cost:
        raise RequestRefusedError(
            400, "query_too_costly", f"the query costs {cost}; at most {settings.max_cost}"
        )


def _extensions(error: GraphQLError) -> dict[str, Any]:
    original = error.original_error
    if isinstance(original, GatewayError):
        extensions: dict[str, Any] = {"code": original.code, "status": original.status}
        if original.retry_after is not None:
            extensions["retryAfter"] = original.retry_after
        return extensions
    if original is not None:
        return {"code": "internal_error", "status": 500}
    return {"code": "invalid_query", "status": 400}


# --- the app -----------------------------------------------------------------------------


async def _request_parts(request: Request) -> tuple[str, dict[str, Any], str | None]:
    if request.method == "GET":
        document = request.query_params.get("query", "")
        raw = request.query_params.get("variables")
        name = request.query_params.get("operationName")
        try:
            variables = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise RequestRefusedError(400, "invalid_query", "variables must be JSON") from exc
    else:
        if not request.headers.get(CSRF_HEADER):
            raise RequestRefusedError(400, "csrf_header_missing", f"send the {CSRF_HEADER} header")
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise RequestRefusedError(415, "unsupported_media_type", "send application/json")
        try:
            body = await request.json()
        except ValueError as exc:
            raise RequestRefusedError(400, "invalid_query", "the body must be JSON") from exc
        if not isinstance(body, dict):
            raise RequestRefusedError(400, "invalid_query", "the body must be a JSON object")
        document, variables, name = (
            body.get("query", ""),
            body.get("variables") or {},
            body.get("operationName"),
        )
    if not isinstance(document, str) or not document or not isinstance(variables, dict):
        raise RequestRefusedError(400, "invalid_query", "send a query and its variables")
    return document, variables, name if isinstance(name, str) else None


def create_app(
    settings: MgrSettings | None = None, *, http_client: httpx.AsyncClient | None = None
) -> FastAPI:
    settings = settings or MgrSettings.from_env()
    configure_logging(f"mgr:{settings.cell_id}")
    http = http_client or httpx.AsyncClient(timeout=settings.timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if http_client is None:
            await http.aclose()

    app = FastAPI(
        title=f"mgr ({settings.cell_id})", lifespan=lifespan, docs_url=None, openapi_url=None
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "mgr",
            "version": __version__,
            "cell_id": settings.cell_id,
        }

    @app.get("/readyz", response_model=None)
    async def readyz() -> dict[str, str] | JSONResponse:
        try:
            response = await http.get(f"{settings.gateway_url.rstrip('/')}/healthz")
        except httpx.HTTPError:
            response = None
        if response is None or response.status_code != 200:
            return JSONResponse(
                {"error": "not_ready", "message": "mgr cannot reach its cell gateway"}, 503
            )
        return {"status": "ready", "service": "mgr"}

    @app.api_route("/graphql", methods=["GET", "POST"])
    async def graphql(request: Request) -> JSONResponse:
        if request.headers.get(INTERNAL_TOKEN_HEADER) != settings.gateway_token:
            return graphql_error(
                401, "unauthenticated", "mgr accepts calls from its cell gateway only"
            )
        tenant = request.headers.get(TENANT_HEADER, "")
        if not is_valid_tenant_id(tenant):
            return graphql_error(400, "invalid_tenant", "no valid tenant")
        try:
            document, variables, name = await _request_parts(request)
            check(document, variables, name, method=request.method, settings=settings)
        except RequestRefusedError as exc:
            return graphql_error(exc.status, exc.code, exc.message)

        gateway = Gateway(
            http, settings.gateway_url, tenant, request.headers.get(REQUEST_ID_HEADER)
        )
        context = Context(gateway=gateway, tenant_id=tenant, cell_id=settings.cell_id)
        result = await schema.execute(
            document, variable_values=variables, context_value=context, operation_name=name
        )
        body: dict[str, Any] = {"data": result.data}
        headers: dict[str, str] = {}
        if result.errors:
            body["errors"] = [{**e.formatted, "extensions": _extensions(e)} for e in result.errors]
            retry = [x["extensions"].get("retryAfter") for x in body["errors"]]
            if any(retry):
                headers["Retry-After"] = str(max(r for r in retry if r))
        return JSONResponse(body, headers=headers)

    return app
