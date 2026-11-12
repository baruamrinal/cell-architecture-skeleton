"""Control plane API: cell registry, tenant onboarding and placement, routing lookups.

Two APIs with separate credentials (docs/plan.md §2.3):
  * admin API (/v1/cells, /v1/tenants)  - admin credential only
  * routing API (/v1/routing/*)         - router credential (read-only) or admin
"""

import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated

import httpx
from fastapi import APIRouter, FastAPI, Path
from pydantic import BaseModel, Field

from cellarch.common.config import ConfigError, parse_pairs, require_env
from cellarch.common.contracts import (
    CELL_ID_PATTERN,
    TENANT_ID_PATTERN,
    CellHosting,
    CellSize,
    CellStatus,
    CellType,
    PlacementMode,
    RoutingEntry,
    TenantStatus,
    Tier,
)
from cellarch.common.errors import ApiError, install_error_handlers
from cellarch.common.logging import configure_logging
from cellarch.common.security import TokenAuth
from cellarch.control_plane.cell_client import CellClient
from cellarch.control_plane.placement import (
    CellCandidate,
    FillFirstPlacement,
    NoCapacityError,
    PlacementStrategy,
)
from cellarch.control_plane.store import ControlPlaneStore, DuplicateError

log = logging.getLogger("cellarch.control_plane")

ADMIN_ROLE = "admin"
ROUTER_ROLE = "router"


@dataclass(frozen=True)
class ControlPlaneSettings:
    db_path: str
    admin_token: str
    router_token: str
    cell_credentials: dict[str, str]
    seed_cells: dict[str, str] = field(default_factory=dict)
    default_cell_capacity: int = 100
    target_utilization: float = 0.75

    @classmethod
    def from_env(cls) -> "ControlPlaneSettings":
        credentials = parse_pairs(os.environ.get("CELL_CREDENTIALS", ""))
        seeds = parse_pairs(os.environ.get("SEED_CELLS", ""))
        missing = sorted(set(seeds) - set(credentials))
        if missing:
            raise ConfigError(f"no credential in CELL_CREDENTIALS for seed cell(s): {missing}")
        return cls(
            db_path=os.environ.get("CONTROL_PLANE_DB_PATH", "./data/control-plane.db"),
            admin_token=require_env("CONTROL_PLANE_ADMIN_TOKEN"),
            router_token=require_env("CONTROL_PLANE_ROUTER_TOKEN"),
            cell_credentials=credentials,
            seed_cells=seeds,
            default_cell_capacity=int(os.environ.get("DEFAULT_CELL_CAPACITY", "100")),
            target_utilization=float(os.environ.get("TARGET_UTILIZATION", "0.75")),
        )


class CellRegistration(BaseModel):
    cell_id: str = Field(pattern=CELL_ID_PATTERN)
    url: str = Field(min_length=1)
    type: CellType = "shared"
    hosting: CellHosting = "vendor"
    size: CellSize = "small"
    capacity: int | None = Field(default=None, ge=1)
    wave: int = Field(default=1, ge=0)


class CellUpdate(BaseModel):
    status: CellStatus | None = None
    capacity: int | None = Field(default=None, ge=1)


class Cell(BaseModel):
    cell_id: str
    url: str
    type: CellType
    hosting: CellHosting
    size: CellSize
    status: CellStatus
    capacity: int
    wave: int
    tenant_count: int
    utilization: float
    created_at: str


class TenantCreate(BaseModel):
    tenant_id: str | None = Field(default=None, pattern=TENANT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    tier: Tier = "standard"


class Tenant(BaseModel):
    tenant_id: str
    name: str
    tier: Tier
    placement_mode: PlacementMode
    cell_id: str
    status: TenantStatus
    created_at: str
    updated_at: str


TenantId = Annotated[str, Path(pattern=TENANT_ID_PATTERN)]


def _cell(row: dict[str, object]) -> Cell:
    capacity = int(str(row["capacity"]))
    count = int(str(row["tenant_count"]))
    return Cell.model_validate({**row, "utilization": round(count / capacity, 4)})


def create_app(
    settings: ControlPlaneSettings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    placement: PlacementStrategy | None = None,
) -> FastAPI:
    settings = settings or ControlPlaneSettings.from_env()
    configure_logging("control-plane")
    store = ControlPlaneStore(settings.db_path)
    for cell_id, url in settings.seed_cells.items():
        store.seed_cell(cell_id, url, settings.default_cell_capacity)

    http = http_client or httpx.AsyncClient(timeout=10.0)
    cells = CellClient(http, settings.cell_credentials)
    strategy = placement or FillFirstPlacement(settings.target_utilization)
    auth = TokenAuth({ADMIN_ROLE: settings.admin_token, ROUTER_ROLE: settings.router_token})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if http_client is None:
            await http.aclose()

    app = FastAPI(title="Control plane", lifespan=lifespan)
    app.state.store = store
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- admin API: cells ----------------------------------------------------------------

    admin = APIRouter(prefix="/v1", dependencies=[auth.require(ADMIN_ROLE)])

    def require_cell(cell_id: str) -> dict[str, object]:
        cell = store.get_cell(cell_id)
        if cell is None:
            raise ApiError(404, "cell_not_found", f"cell {cell_id} not found")
        return cell

    @admin.post("/cells", status_code=201)
    def register_cell(body: CellRegistration) -> Cell:
        if not cells.has_credential(body.cell_id):
            raise ApiError(
                422,
                "missing_cell_credential",
                f"no credential is configured for {body.cell_id}; add it before registering",
            )
        try:
            row = store.add_cell(
                cell_id=body.cell_id,
                url=body.url,
                type=body.type,
                hosting=body.hosting,
                size=body.size,
                capacity=body.capacity or settings.default_cell_capacity,
                wave=body.wave,
            )
        except DuplicateError as exc:
            raise ApiError(409, "cell_exists", f"cell {body.cell_id} already exists") from exc
        return _cell(row)

    @admin.get("/cells")
    def list_cells() -> list[Cell]:
        return [_cell(row) for row in store.list_cells()]

    @admin.get("/cells/{cell_id}")
    def get_cell(cell_id: str) -> Cell:
        return _cell(require_cell(cell_id))

    @admin.patch("/cells/{cell_id}")
    def update_cell(cell_id: str, body: CellUpdate) -> Cell:
        require_cell(cell_id)
        row = store.update_cell(cell_id, status=body.status, capacity=body.capacity)
        return _cell(row or require_cell(cell_id))

    # --- admin API: tenants --------------------------------------------------------------

    def require_tenant(tenant_id: str) -> dict[str, object]:
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            raise ApiError(404, "tenant_not_found", f"tenant {tenant_id} not found")
        return tenant

    @admin.post("/tenants", status_code=201)
    async def create_tenant(body: TenantCreate) -> Tenant:
        tenant_id = body.tenant_id or f"t-{uuid.uuid4().hex[:12]}"
        if store.get_tenant(tenant_id) is not None:
            raise ApiError(409, "tenant_exists", f"tenant {tenant_id} already exists")

        candidates = [
            CellCandidate(
                cell_id=str(c["cell_id"]),
                type=str(c["type"]),
                status=str(c["status"]),
                capacity=int(c["capacity"]),
                tenant_count=int(c["tenant_count"]),
            )
            for c in store.list_cells()
        ]
        try:
            decision = strategy.choose(candidates)
        except NoCapacityError as exc:
            log.error("no capacity for tenant %s", tenant_id)
            raise ApiError(503, "no_capacity", "no cell has room for a new tenant") from exc
        if decision.over_target:
            log.warning(
                "capacity low: placing %s on %s past the %.0f%% target; add a cell",
                tenant_id,
                decision.cell_id,
                settings.target_utilization * 100,
            )

        # Placement and insert are not atomic; acceptable for a single control plane
        # instance (plan §2.1). A shared database needs a transactional reservation.
        try:
            store.add_tenant(tenant_id, body.name, body.tier, decision.cell_id)
        except DuplicateError as exc:  # pragma: no cover - concurrent create of the same id
            raise ApiError(409, "tenant_exists", f"tenant {tenant_id} already exists") from exc

        cell = require_cell(decision.cell_id)
        try:
            await cells.provision_tenant(decision.cell_id, str(cell["url"]), tenant_id)
        except Exception as exc:
            store.delete_tenant(tenant_id)
            log.warning("provisioning %s on %s failed: %s", tenant_id, decision.cell_id, exc)
            raise ApiError(
                502, "provisioning_failed", f"cell {decision.cell_id} did not provision the tenant"
            ) from exc

        store.set_tenant_status(tenant_id, "active")
        log.info("tenant %s placed on %s", tenant_id, decision.cell_id)
        return Tenant.model_validate(require_tenant(tenant_id))

    @admin.get("/tenants")
    def list_tenants() -> list[Tenant]:
        return [Tenant.model_validate(t) for t in store.list_tenants()]

    @admin.get("/tenants/{tenant_id}")
    def get_tenant(tenant_id: str) -> Tenant:
        return Tenant.model_validate(require_tenant(tenant_id))

    # --- routing API (router: read-only) -------------------------------------------------

    routing = APIRouter(prefix="/v1/routing", dependencies=[auth.require(ROUTER_ROLE, ADMIN_ROLE)])

    @routing.get("/tenants/{tenant_id}")
    def route_tenant(tenant_id: TenantId) -> RoutingEntry:
        entry = store.routing_entry(tenant_id)
        if entry is None:
            raise ApiError(404, "tenant_not_found", f"tenant {tenant_id} not found")
        return RoutingEntry.model_validate(entry)

    app.include_router(admin)
    app.include_router(routing)
    return app
