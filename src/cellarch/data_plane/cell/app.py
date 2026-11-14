"""Cell gateway: the cell's single entry point (docs/plan.md §2.2, §3.4).

* ``/v1/users…`` and ``/v1/orders…`` go to the calling tenant's own user-account and
  order-processing services, with the tenant's stack credential.
* Tenants the cell doesn't host get ``421`` (the router's route is stale).
* ``/internal/*`` (control plane only, this cell's credential) runs the cell's part of
  onboarding: create the tenant's database, logins and stack secret, activate the
  tenant once its stack is ready, and delete everything again; and of tenant moves
  (docs/plan.md §5): freeze the tenant (reads only), export its data, import it on the
  target, unfreeze it after a rollback.
"""

import asyncio
import contextlib
import itertools
import os
import string
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from cellarch.common.config import require_env
from cellarch.common.contracts import TENANT_ID_PATTERN, is_valid_tenant_id
from cellarch.common.errors import ApiError, install_error_handlers
from cellarch.common.headers import CELL_HEADER, INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.common.logging import configure_logging
from cellarch.common.security import TokenAuth
from cellarch.common.sql import DatabaseError
from cellarch.data_plane.cell.concurrency import ConcurrencyLimit
from cellarch.data_plane.cell.store import CellStore
from cellarch.data_plane.cell.tenant_data import TenantDataError, export_tenant, import_tenant
from cellarch.data_plane.cell.tenant_resources import (
    SERVICES,
    STACK_TOKEN_KEY,
    KubernetesSecrets,
    TenantResourceError,
    TenantSecrets,
    database_url_key,
    open_tenant_databases,
)

CONTROL_PLANE_ROLE = "control-plane"
DEFAULT_SERVICE_URL = "http://tenant-{tenant}-{service}:8000"  # tenant-stack chart's Services
# Which service owns each public resource.
RESOURCES = {"users": "user-account", "orders": "order-processing"}
FORWARDED_REQUEST_HEADERS = ("content-type", "accept", "idempotency-key", "x-request-id")
FORWARDED_RESPONSE_HEADERS = ("content-type", "idempotent-replay", "retry-after")
RETRY_AFTER = {"Retry-After": "1"}
READ_METHODS = frozenset({"GET", "HEAD"})
FROZEN = "frozen"  # being moved: reads only


def _check_service_url(template: str) -> None:
    fields = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    if fields != {"tenant", "service"}:
        raise ValueError("TENANT_SERVICE_URL must use exactly {tenant} and {service}")


@dataclass(frozen=True)
class CellSettings:
    cell_id: str
    db_path: str  # the gateway's tenant registry
    internal_token: str = field(repr=False)
    # The cell's database server, as its admin login (sqlite:///<dir> in-process).
    tenant_db_admin_url: str = field(default="sqlite:///./data/tenants", repr=False)
    # Where each tenant's services answer.
    tenant_service_url: str = DEFAULT_SERVICE_URL
    # Requests handled at once; more are rejected with 503 cell_overloaded.
    max_concurrent_requests: int = 100
    stack_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        _check_service_url(self.tenant_service_url)
        open_tenant_databases(self.tenant_db_admin_url)  # validates the URL

    @classmethod
    def from_env(cls) -> "CellSettings":
        cell_id = require_env("CELL_ID")
        return cls(
            cell_id=cell_id,
            db_path=os.environ.get("CELL_DB_PATH", f"./data/{cell_id}.db"),
            internal_token=require_env("CELL_INTERNAL_TOKEN"),
            tenant_db_admin_url=require_env("TENANT_DB_ADMIN_URL"),
            tenant_service_url=os.environ.get("TENANT_SERVICE_URL", DEFAULT_SERVICE_URL),
            max_concurrent_requests=int(os.environ.get("MAX_CONCURRENT_REQUESTS", "100")),
            stack_timeout_seconds=float(os.environ.get("STACK_TIMEOUT", "10")),
        )


class TenantProvision(BaseModel):
    tenant_id: str = Field(pattern=TENANT_ID_PATTERN)


class CellTenant(BaseModel):
    tenant_id: str
    status: str
    provisioned_at: str


def create_app(
    settings: CellSettings | None = None,
    *,
    secrets: TenantSecrets | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or CellSettings.from_env()
    configure_logging(f"cell:{settings.cell_id}")
    store = CellStore(settings.db_path)
    databases = open_tenant_databases(settings.tenant_db_admin_url)
    stack_secrets = secrets or KubernetesSecrets.in_cluster()
    http = http_client or httpx.AsyncClient(timeout=settings.stack_timeout_seconds)
    auth = TokenAuth({CONTROL_PLANE_ROLE: settings.internal_token})

    def service_url(tenant_id: str, service: str) -> str:
        return settings.tenant_service_url.format(tenant=tenant_id, service=service)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if http_client is None:
            await http.aclose()

    app = FastAPI(title=f"Cell {settings.cell_id}", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    install_error_handlers(app)
    app.add_middleware(
        ConcurrencyLimit,
        max_concurrent=settings.max_concurrent_requests,
        cell_id=settings.cell_id,
    )

    @app.middleware("http")
    async def stamp_cell_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers[CELL_HEADER] = settings.cell_id
        return response

    def tenant_id(
        value: Annotated[str | None, Header(alias=TENANT_HEADER)] = None,
    ) -> str:
        """Resolve and authorise the calling tenant for this cell."""
        if not value:
            raise ApiError(400, "missing_tenant", f"{TENANT_HEADER} header is required")
        if not is_valid_tenant_id(value):
            raise ApiError(400, "invalid_tenant", "malformed tenant id")
        if store.get_tenant(value) is None:
            # 421 Misdirected Request: tells the router its route is stale.
            raise ApiError(421, "tenant_not_in_cell", f"tenant is not hosted on {settings.cell_id}")
        return value

    Tenant = Annotated[str, Depends(tenant_id)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "cell_id": settings.cell_id}

    # --- tenant API: forwarded to the tenant's own stack --------------------------------

    async def forward(request: Request, tenant: str, resource: str, rest: str) -> Response:
        entry = store.get_tenant(tenant)
        status = entry["status"] if entry else None
        if status == FROZEN and request.method not in READ_METHODS:
            raise ApiError(
                503, "tenant_migrating", "the tenant is being moved; retry shortly", RETRY_AFTER
            )
        if status not in ("active", FROZEN):
            raise ApiError(503, "tenant_not_ready", "the tenant is being set up", RETRY_AFTER)
        headers = {k: v for k, v in request.headers.items() if k in FORWARDED_REQUEST_HEADERS}
        # The gateway, not the client, presents the tenant's stack credential.
        headers[INTERNAL_TOKEN_HEADER] = store.stack_token(tenant) or ""
        headers[TENANT_HEADER] = tenant
        url = f"{service_url(tenant, RESOURCES[resource])}/v1/{resource}{rest}"
        try:
            upstream = await http.request(
                request.method,
                url,
                params=request.query_params,
                content=await request.body(),
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ApiError(
                503,
                "tenant_stack_unavailable",
                "the tenant's services are unavailable",
                RETRY_AFTER,
            ) from exc
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={k: v for k, v in upstream.headers.items() if k in FORWARDED_RESPONSE_HEADERS},
        )

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    for resource in RESOURCES:

        async def route(
            request: Request, tenant: Tenant, rest: str, resource: str = resource
        ) -> Response:
            return await forward(request, tenant, resource, rest)

        app.add_api_route(f"/v1/{resource}{{rest:path}}", route, methods=methods)

    # --- internal API (control plane only, this cell's credential) ----------------------

    internal = APIRouter(prefix="/internal", dependencies=[auth.require(CONTROL_PLANE_ROLE)])

    @internal.post("/tenants", status_code=201)
    def provision_tenant(body: TenantProvision) -> CellTenant:
        """Create the tenant's database, logins and stack secret. Idempotent."""
        tenant = body.tenant_id
        existing = store.get_tenant(tenant)
        if existing is not None:
            return CellTenant(**existing)
        token = token_urlsafe(32)
        try:
            urls = databases.create(tenant)
            stack_secrets.put(
                tenant,
                {STACK_TOKEN_KEY: token, **{database_url_key(s): urls[s] for s in SERVICES}},
            )
        except TenantResourceError as exc:
            for undo in (databases.drop, stack_secrets.delete):
                with contextlib.suppress(TenantResourceError):  # best-effort clean-up
                    undo(tenant)
            raise ApiError(502, "tenant_provisioning_failed", str(exc)) from exc
        return CellTenant(**store.add_tenant(tenant, token))

    @internal.get("/tenants")
    def list_tenants() -> list[CellTenant]:
        return [CellTenant(**t) for t in store.list_tenants()]

    @internal.post("/tenants/{tenant_id}/activate")
    async def activate_tenant(tenant_id: str) -> CellTenant:
        """Mark the tenant active once every service of its stack is ready."""
        if store.get_tenant(tenant_id) is None:
            raise ApiError(404, "tenant_not_found", f"tenant {tenant_id} is not on this cell")

        async def ready(service: str) -> bool:
            try:
                response = await http.get(f"{service_url(tenant_id, service)}/readyz")
            except httpx.HTTPError:
                return False
            return response.status_code == 200

        if not all(await asyncio.gather(*(ready(s) for s in SERVICES))):
            raise ApiError(409, "tenant_stack_not_ready", "the tenant's services are not ready yet")
        store.set_status(tenant_id, "active")
        tenant = store.get_tenant(tenant_id)
        return CellTenant(**tenant)  # type: ignore[arg-type]

    def hosted(tenant_id: str) -> dict[str, Any]:
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            raise ApiError(404, "tenant_not_found", f"tenant {tenant_id} is not on this cell")
        return tenant

    @internal.post("/tenants/{tenant_id}/freeze")
    def freeze_tenant(tenant_id: str) -> CellTenant:
        """Stop taking writes for the tenant (a move is starting). Idempotent."""
        tenant = hosted(tenant_id)
        if tenant["status"] not in ("active", FROZEN):
            raise ApiError(409, "tenant_not_active", "only an active tenant can be frozen")
        store.set_status(tenant_id, FROZEN)
        return CellTenant(**hosted(tenant_id))

    @internal.post("/tenants/{tenant_id}/unfreeze")
    def unfreeze_tenant(tenant_id: str) -> CellTenant:
        """Take writes again (the move was rolled back). Idempotent."""
        if hosted(tenant_id)["status"] == FROZEN:
            store.set_status(tenant_id, "active")
        return CellTenant(**hosted(tenant_id))

    @internal.get("/tenants/{tenant_id}/export")
    def export(tenant_id: str) -> StreamingResponse:
        """The frozen tenant's data as JSON lines (tenant_data.py)."""
        if hosted(tenant_id)["status"] != FROZEN:
            raise ApiError(409, "tenant_not_frozen", "freeze the tenant before exporting it")
        try:
            lines = export_tenant(databases, tenant_id)
            first = next(lines)  # fail before the response starts if the database can't
        except DatabaseError as exc:
            raise ApiError(502, "tenant_export_failed", "the tenant's database failed") from exc
        except TenantDataError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from exc
        return StreamingResponse(itertools.chain([first], lines), media_type="application/x-ndjson")

    @internal.post("/tenants/{tenant_id}/import")
    async def import_(tenant_id: str, request: Request) -> dict[str, dict[str, int]]:
        """Load another cell's export into this tenant's new, empty database."""
        if hosted(tenant_id)["status"] != "provisioning":
            raise ApiError(
                409, "tenant_not_importable", "only a tenant being set up here can be imported"
            )
        body = await request.body()
        try:
            counts = await run_in_threadpool(import_tenant, databases, tenant_id, body.splitlines())
        except TenantDataError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from exc
        except DatabaseError as exc:
            raise ApiError(502, "tenant_import_failed", "the tenant's database failed") from exc
        return {"tables": counts}

    @internal.delete("/tenants/{tenant_id}", status_code=204)
    def delete_tenant(tenant_id: str) -> Response:
        """Drop the tenant's database, logins and secret. Idempotent."""
        if is_valid_tenant_id(tenant_id):
            try:
                databases.drop(tenant_id)
                stack_secrets.delete(tenant_id)
            except TenantResourceError as exc:
                raise ApiError(502, "tenant_deletion_failed", str(exc)) from exc
        store.delete_tenant(tenant_id)
        return Response(status_code=204)

    app.include_router(internal)
    return app
