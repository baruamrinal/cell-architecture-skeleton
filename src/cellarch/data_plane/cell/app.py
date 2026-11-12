"""Cell application: a sample tenant API plus /internal endpoints for the control plane."""

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from pydantic import BaseModel, Field

from cellarch.common.config import require_env
from cellarch.common.contracts import TENANT_ID_PATTERN, Isolation, is_valid_tenant_id
from cellarch.common.errors import ApiError, install_error_handlers
from cellarch.common.headers import CELL_HEADER, TENANT_HEADER
from cellarch.common.logging import configure_logging
from cellarch.common.security import TokenAuth
from cellarch.data_plane.cell.concurrency import ConcurrencyLimit
from cellarch.data_plane.cell.store import CellStore

CONTROL_PLANE_ROLE = "control-plane"


@dataclass(frozen=True)
class CellSettings:
    cell_id: str
    db_path: str
    internal_token: str
    # Requests handled at once; more are rejected with 503 cell_overloaded.
    max_concurrent_requests: int = 100

    @classmethod
    def from_env(cls) -> "CellSettings":
        cell_id = require_env("CELL_ID")
        return cls(
            cell_id=cell_id,
            db_path=os.environ.get("CELL_DB_PATH", f"./data/{cell_id}.db"),
            internal_token=require_env("CELL_INTERNAL_TOKEN"),
            max_concurrent_requests=int(os.environ.get("MAX_CONCURRENT_REQUESTS", "100")),
        )


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class Item(BaseModel):
    id: str
    tenant_id: str
    name: str
    payload: dict[str, Any]
    created_at: str


class TenantProvision(BaseModel):
    tenant_id: str = Field(pattern=TENANT_ID_PATTERN)
    isolation: Isolation = "pooled"


class CellTenant(BaseModel):
    tenant_id: str
    status: str
    provisioned_at: str
    isolation: Isolation


def create_app(settings: CellSettings | None = None) -> FastAPI:
    settings = settings or CellSettings.from_env()
    configure_logging(f"cell:{settings.cell_id}")
    store = CellStore(settings.db_path)
    auth = TokenAuth({CONTROL_PLANE_ROLE: settings.internal_token})

    app = FastAPI(title=f"Cell {settings.cell_id}")
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

    # --- tenant API ---------------------------------------------------------------------

    @app.post("/v1/items", status_code=201)
    def create_item(body: ItemCreate, tenant: Tenant) -> Item:
        return Item(**store.create_item(tenant, body.name, body.payload))

    @app.get("/v1/items")
    def list_items(tenant: Tenant, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[Item]:
        return [Item(**item) for item in store.list_items(tenant, limit)]

    @app.get("/v1/items/{item_id}")
    def get_item(item_id: str, tenant: Tenant) -> Item:
        item = store.get_item(tenant, item_id)
        if item is None:
            raise ApiError(404, "item_not_found", "item not found")
        return Item(**item)

    @app.delete("/v1/items/{item_id}", status_code=204)
    def delete_item(item_id: str, tenant: Tenant) -> Response:
        if not store.delete_item(tenant, item_id):
            raise ApiError(404, "item_not_found", "item not found")
        return Response(status_code=204)

    # --- internal API (control plane only, this cell's credential) ----------------------

    internal = APIRouter(prefix="/internal", dependencies=[auth.require(CONTROL_PLANE_ROLE)])

    @internal.post("/tenants", status_code=201)
    def provision_tenant(body: TenantProvision) -> CellTenant:
        return CellTenant(**store.provision_tenant(body.tenant_id, body.isolation))

    @internal.get("/tenants")
    def list_tenants() -> list[CellTenant]:
        return [CellTenant(**t) for t in store.list_tenants()]

    @internal.delete("/tenants/{tenant_id}", status_code=204)
    def delete_tenant(tenant_id: str) -> Response:
        store.delete_tenant(tenant_id)  # idempotent
        return Response(status_code=204)

    app.include_router(internal)
    return app
