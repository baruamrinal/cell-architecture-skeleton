"""Skeleton every tenant service shares: error shape, health probes, auth, tenant check.

* ``/healthz`` - liveness: the process is up.
* ``/readyz``  - readiness: the service can reach its database (dependency problems
  that only affect some requests, like user-account being down, don't make it unready).
* ``/v1/*``    - only the tenant's own stack may call it (stack credential), and only
  for the tenant this instance serves.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header
from fastapi.responses import Response

from cellarch.common.errors import ApiError, error_response, install_error_handlers
from cellarch.common.headers import TENANT_HEADER
from cellarch.common.logging import configure_logging
from cellarch.common.security import TokenAuth
from cellarch.common.sql import Database, DatabaseError
from cellarch.services.common.settings import ServiceSettings

STACK_ROLE = "stack"

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_service_app(
    name: str, settings: ServiceSettings, database: Database, lifespan: Lifespan | None = None
) -> FastAPI:
    configure_logging(f"{name}:{settings.tenant_id}")
    app = FastAPI(title=f"{name} ({settings.tenant_id})", lifespan=lifespan)
    app.state.database = database
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": name, "tenant_id": settings.tenant_id}

    @app.get("/readyz", response_model=None)
    def readyz() -> dict[str, str] | Response:
        try:
            app.state.database.ping()
        except DatabaseError:
            return error_response(503, "not_ready", f"{name} cannot reach its database")
        return {"status": "ready", "service": name}

    return app


def tenant_api(settings: ServiceSettings) -> APIRouter:
    """Router for ``/v1``: stack credential first, then the tenant check."""
    auth = TokenAuth({STACK_ROLE: settings.stack_token})

    def this_tenant(tenant_id: Annotated[str | None, Header(alias=TENANT_HEADER)] = None) -> None:
        if not tenant_id:
            raise ApiError(400, "missing_tenant", f"{TENANT_HEADER} header is required")
        if tenant_id != settings.tenant_id:
            raise ApiError(421, "tenant_mismatch", "this service serves a different tenant")

    return APIRouter(prefix="/v1", dependencies=[auth.require(STACK_ROLE), Depends(this_tenant)])
