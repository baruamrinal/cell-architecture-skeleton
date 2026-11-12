"""Control plane API: cell registry, tenant onboarding and placement, routing lookups.

Two APIs with separate credentials (docs/plan.md §2.3):
  * admin API (/v1/cells, /v1/tenants)  - admin credential only
  * routing API (/v1/routing/*)         - router credential (read-only) or admin
"""

import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Path, Response
from pydantic import BaseModel, Field

from cellarch.common.config import ConfigError, parse_pairs, require_env
from cellarch.common.contracts import (
    CELL_ID_PATTERN,
    TENANT_ID_PATTERN,
    CellHosting,
    CellSize,
    CellStatus,
    CellType,
    Isolation,
    PlacementMode,
    RateLimit,
    RoutingEntry,
    TenantStatus,
    Tier,
)
from cellarch.common.errors import ApiError, install_error_handlers
from cellarch.common.headers import ACTOR_HEADER
from cellarch.common.logging import configure_logging
from cellarch.common.security import TokenAuth
from cellarch.control_plane.cell_client import CellClient, CellVerificationError
from cellarch.control_plane.credentials import LocalCredentialStore
from cellarch.control_plane.placement import (
    CellCandidate,
    FillFirstPlacement,
    NoCapacityError,
    PlacementStrategy,
)
from cellarch.control_plane.store import ControlPlaneStore, DuplicateError

log = logging.getLogger("cellarch.control_plane")

ADMIN_ROLE = "admin"

_ACTOR_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")


def _isolation(value: str) -> Isolation:
    if value == "pooled":
        return "pooled"
    if value == "isolated":
        return "isolated"
    raise ConfigError(f"DEFAULT_TENANT_ISOLATION must be 'pooled' or 'isolated', not {value!r}")


def actor_label(role: str, actor: str | None) -> str:
    """``role:name`` for created_by / updated_by, or just ``role`` without a name.

    The role comes from the verified credential; the name is self-reported locally
    (X-Actor) and becomes the verified IAM identity on AWS (stage 5).
    """
    if not actor:
        return role
    if not _ACTOR_NAME.fullmatch(actor):
        raise ValueError("invalid caller name")
    return f"{role}:{actor}"


ROUTER_ROLE = "router"

# Per-tier defaults: (requests per second, burst). Overridable per tenant.
DEFAULT_TIER_LIMITS = {
    "standard": RateLimit(rps=50, burst=100),
    "premium": RateLimit(rps=500, burst=1000),
}


def parse_rate_limit(name: str, raw: str) -> RateLimit:
    """Parse ``"<requests per second>:<burst>"``, e.g. ``"50:100"``."""
    rps, separator, burst = raw.partition(":")
    try:
        if not separator:
            raise ValueError
        return RateLimit(rps=float(rps.strip()), burst=int(burst.strip()))
    except ValueError as exc:  # pydantic's ValidationError is a ValueError too
        raise ConfigError(
            f"{name} must be '<requests per second>:<burst>', e.g. 50:100 (got {raw!r})"
        ) from exc


@dataclass(frozen=True)
class ControlPlaneSettings:
    db_path: str
    admin_token: str
    router_token: str
    cell_credentials: dict[str, str]
    seed_cells: dict[str, str] = field(default_factory=dict)
    default_cell_capacity: int = 100
    target_utilization: float = 0.75
    # Each tenant gets its own database in its cell unless onboarded as pooled.
    default_isolation: Isolation = "isolated"
    tier_limits: dict[str, RateLimit] = field(default_factory=lambda: dict(DEFAULT_TIER_LIMITS))

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
            default_isolation=_isolation(os.environ.get("DEFAULT_TENANT_ISOLATION", "isolated")),
            tier_limits={
                tier: parse_rate_limit(
                    f"RATE_LIMIT_{tier.upper()}",
                    os.environ.get(f"RATE_LIMIT_{tier.upper()}", f"{limit.rps:g}:{limit.burst}"),
                )
                for tier, limit in DEFAULT_TIER_LIMITS.items()
            },
        )


class CellRegistration(BaseModel):
    cell_id: str = Field(pattern=CELL_ID_PATTERN)
    url: str = Field(min_length=1)
    type: CellType = "shared"
    hosting: CellHosting = "vendor"
    size: CellSize = "small"
    capacity: int | None = Field(default=None, ge=1)
    wave: int = Field(default=1, ge=0)
    # Write-only: stored for calling the cell, never returned. Optional when the
    # credential is configured (CELL_CREDENTIALS).
    credential: str | None = Field(default=None, min_length=16, max_length=512, repr=False)


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
    isolated_tenants: int
    utilization: float
    created_at: str
    created_by: str
    updated_at: str
    updated_by: str


class TenantCreate(BaseModel):
    tenant_id: str | None = Field(default=None, pattern=TENANT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    tier: Tier = "standard"
    isolation: Isolation | None = None  # default: ControlPlaneSettings.default_isolation


class TenantRateLimit(BaseModel):
    rps: float
    burst: int
    source: Literal["tier", "override"]


class Tenant(BaseModel):
    tenant_id: str
    name: str
    tier: Tier
    placement_mode: PlacementMode
    isolation: Isolation
    cell_id: str
    status: TenantStatus
    created_at: str
    created_by: str
    updated_at: str
    updated_by: str
    rate_limit: TenantRateLimit


class StatusWarning(BaseModel):
    code: str
    message: str


class StatusTotals(BaseModel):
    cells: int
    placeable_cells: int  # shared and active: where new tenants can go
    tenants: int
    capacity: int  # across placeable cells
    free_slots: int  # across placeable cells
    isolated_tenants: int  # tenants with their own database


class PlatformStatus(BaseModel):
    target_utilization: float
    totals: StatusTotals
    cells: list[Cell]
    warnings: list[StatusWarning]


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
    credentials = LocalCredentialStore(settings.db_path, settings.cell_credentials)
    cells = CellClient(http, credentials)
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

    async def current_actor(
        role: str = auth.require(ADMIN_ROLE),
        actor: Annotated[str | None, Header(alias=ACTOR_HEADER)] = None,
    ) -> str:
        try:
            return actor_label(role, actor)
        except ValueError as exc:
            raise ApiError(
                400,
                "invalid_actor",
                f"{ACTOR_HEADER} must be 1-64 characters: letters, digits, '.', '_', '@', '-'",
            ) from exc

    # Every admin call is authenticated and has its caller name checked.
    admin = APIRouter(prefix="/v1", dependencies=[Depends(current_actor)])

    def require_cell(cell_id: str) -> dict[str, object]:
        cell = store.get_cell(cell_id)
        if cell is None:
            raise ApiError(404, "cell_not_found", f"cell {cell_id} not found")
        return cell

    @admin.post("/cells", status_code=201)
    async def register_cell(
        body: CellRegistration, actor: Annotated[str, Depends(current_actor)]
    ) -> Cell:
        if store.get_cell(body.cell_id) is not None:
            raise ApiError(409, "cell_exists", f"cell {body.cell_id} already exists")
        credential = body.credential or credentials.get(body.cell_id)
        if not credential:
            raise ApiError(
                422,
                "missing_cell_credential",
                f"no credential for {body.cell_id}: pass one, or configure CELL_CREDENTIALS",
            )
        try:
            await cells.verify(body.cell_id, body.url, credential)
        except CellVerificationError as exc:
            raise ApiError(422, exc.code, exc.message) from exc

        if body.credential:
            credentials.put(body.cell_id, body.credential)
        try:
            row = store.add_cell(
                cell_id=body.cell_id,
                url=body.url,
                type=body.type,
                hosting=body.hosting,
                size=body.size,
                capacity=body.capacity or settings.default_cell_capacity,
                wave=body.wave,
                actor=actor,
            )
        except DuplicateError as exc:  # pragma: no cover - concurrent register of the same id
            raise ApiError(409, "cell_exists", f"cell {body.cell_id} already exists") from exc
        log.info("cell %s registered at %s", body.cell_id, body.url)
        return _cell(row)

    @admin.get("/cells")
    def list_cells() -> list[Cell]:
        return [_cell(row) for row in store.list_cells()]

    @admin.get("/cells/{cell_id}")
    def get_cell(cell_id: str) -> Cell:
        return _cell(require_cell(cell_id))

    @admin.patch("/cells/{cell_id}")
    def update_cell(
        cell_id: str, body: CellUpdate, actor: Annotated[str, Depends(current_actor)]
    ) -> Cell:
        cell = require_cell(cell_id)
        tenants = int(str(cell["tenant_count"]))
        if body.capacity is not None and body.capacity < tenants:
            raise ApiError(
                409,
                "capacity_below_tenants",
                f"{cell_id} hosts {tenants} tenants; capacity can't be lower than that",
            )
        row = store.update_cell(cell_id, status=body.status, capacity=body.capacity, actor=actor)
        return _cell(row or require_cell(cell_id))

    @admin.delete("/cells/{cell_id}", status_code=204)
    def decommission_cell(cell_id: str) -> Response:
        cell = require_cell(cell_id)
        tenants = int(str(cell["tenant_count"]))
        if tenants:
            raise ApiError(
                409,
                "cell_not_empty",
                f"{cell_id} still hosts {tenants} tenants; move them off first",
            )
        store.delete_cell(cell_id)
        credentials.delete(cell_id)
        log.info("cell %s decommissioned", cell_id)
        return Response(status_code=204)

    def candidates() -> list[CellCandidate]:
        return [
            CellCandidate(
                cell_id=str(c["cell_id"]),
                type=str(c["type"]),
                status=str(c["status"]),
                capacity=int(c["capacity"]),
                tenant_count=int(c["tenant_count"]),
            )
            for c in store.list_cells()
        ]

    @admin.get("/status")
    def platform_status() -> PlatformStatus:
        rows = store.list_cells()
        placeable = [c for c in rows if c["type"] == "shared" and c["status"] == "active"]
        warnings: list[StatusWarning] = []
        try:
            decision = strategy.choose(candidates())
        except NoCapacityError:
            warnings.append(
                StatusWarning(
                    code="no_capacity",
                    message="No shared active cell has room: new tenants will be refused. "
                    "Deploy a cell or raise a cell's capacity.",
                )
            )
        else:
            if decision.over_target:
                warnings.append(
                    StatusWarning(
                        code="capacity_low",
                        message=f"Every shared active cell is at or past the "
                        f"{settings.target_utilization:.0%} target; new tenants use the "
                        "remaining room. Deploy another cell soon.",
                    )
                )
        return PlatformStatus(
            target_utilization=settings.target_utilization,
            totals=StatusTotals(
                cells=len(rows),
                placeable_cells=len(placeable),
                tenants=sum(int(c["tenant_count"]) for c in rows),
                capacity=sum(int(c["capacity"]) for c in placeable),
                free_slots=sum(
                    max(0, int(c["capacity"]) - int(c["tenant_count"])) for c in placeable
                ),
                isolated_tenants=sum(int(c["isolated_tenants"]) for c in rows),
            ),
            cells=[_cell(row) for row in rows],
            warnings=warnings,
        )

    # --- admin API: tenants --------------------------------------------------------------

    def effective_limit(row: dict[str, object]) -> tuple[RateLimit, Literal["tier", "override"]]:
        rps, burst = row.get("rate_limit_rps"), row.get("rate_limit_burst")
        if rps is not None and burst is not None:
            return RateLimit(rps=float(str(rps)), burst=int(str(burst))), "override"
        return settings.tier_limits[str(row["tier"])], "tier"

    def tenant_out(row: dict[str, object]) -> Tenant:
        limit, source = effective_limit(row)
        rate_limit = {"rps": limit.rps, "burst": limit.burst, "source": source}
        return Tenant.model_validate({**row, "rate_limit": rate_limit})

    def require_tenant(tenant_id: str) -> dict[str, object]:
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            raise ApiError(404, "tenant_not_found", f"tenant {tenant_id} not found")
        return tenant

    @admin.post("/tenants", status_code=201)
    async def create_tenant(
        body: TenantCreate, actor: Annotated[str, Depends(current_actor)]
    ) -> Tenant:
        tenant_id = body.tenant_id or f"t-{uuid.uuid4().hex[:12]}"
        isolation = body.isolation or settings.default_isolation
        if store.get_tenant(tenant_id) is not None:
            raise ApiError(409, "tenant_exists", f"tenant {tenant_id} already exists")

        try:
            decision = strategy.choose(candidates())
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
            store.add_tenant(tenant_id, body.name, body.tier, decision.cell_id, actor, isolation)
        except DuplicateError as exc:  # pragma: no cover - concurrent create of the same id
            raise ApiError(409, "tenant_exists", f"tenant {tenant_id} already exists") from exc

        cell = require_cell(decision.cell_id)
        try:
            await cells.provision_tenant(decision.cell_id, str(cell["url"]), tenant_id, isolation)
        except Exception as exc:
            store.delete_tenant(tenant_id)
            log.warning("provisioning %s on %s failed: %s", tenant_id, decision.cell_id, exc)
            raise ApiError(
                502, "provisioning_failed", f"cell {decision.cell_id} did not provision the tenant"
            ) from exc

        store.set_tenant_status(tenant_id, "active", actor)
        log.info("tenant %s placed on %s", tenant_id, decision.cell_id)
        return tenant_out(require_tenant(tenant_id))

    @admin.get("/tenants")
    def list_tenants() -> list[Tenant]:
        return [tenant_out(t) for t in store.list_tenants()]

    @admin.get("/tenants/{tenant_id}")
    def get_tenant(tenant_id: str) -> Tenant:
        return tenant_out(require_tenant(tenant_id))

    @admin.put("/tenants/{tenant_id}/rate-limit")
    def set_tenant_rate_limit(
        tenant_id: TenantId, body: RateLimit, actor: Annotated[str, Depends(current_actor)]
    ) -> Tenant:
        require_tenant(tenant_id)
        store.set_rate_limit(tenant_id, body.rps, body.burst, actor)
        log.info("rate limit of %s set to %s/s burst %s", tenant_id, body.rps, body.burst)
        return tenant_out(require_tenant(tenant_id))

    @admin.delete("/tenants/{tenant_id}/rate-limit")
    def clear_tenant_rate_limit(
        tenant_id: TenantId, actor: Annotated[str, Depends(current_actor)]
    ) -> Tenant:
        require_tenant(tenant_id)
        store.set_rate_limit(tenant_id, None, None, actor)
        log.info("rate limit override of %s cleared", tenant_id)
        return tenant_out(require_tenant(tenant_id))

    # --- routing API (router: read-only) -------------------------------------------------

    routing = APIRouter(prefix="/v1/routing", dependencies=[auth.require(ROUTER_ROLE, ADMIN_ROLE)])

    @routing.get("/tenants/{tenant_id}")
    def route_tenant(tenant_id: TenantId) -> RoutingEntry:
        entry = store.routing_entry(tenant_id)
        if entry is None:
            raise ApiError(404, "tenant_not_found", f"tenant {tenant_id} not found")
        limit, _ = effective_limit(entry)
        return RoutingEntry.model_validate({**entry, "rate_limit": limit.model_dump()})

    app.include_router(admin)
    app.include_router(routing)
    return app
