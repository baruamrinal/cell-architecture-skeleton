"""Router: resolves the tenant's cell and forwards the request there.

Stateless apart from the route cache. Never exposes cells' /internal endpoints and
never forwards internal credentials from clients.

The web UI's ``/graphql`` (docs/plan.md §2.4) is routed the same way, but its tenant
comes from the host (``acme.platform.com``) and must match a verified session token;
a client's own ``X-Tenant-ID``, cookie and ``Authorization`` never reach the cell.
"""

import os
import posixpath
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote, unquote

import httpx
from fastapi import FastAPI, Request, Response

from cellarch.common.clock import Clock, monotonic
from cellarch.common.config import require_env
from cellarch.common.contracts import RoutingEntry, is_valid_tenant_id
from cellarch.common.errors import GRAPHQL_PATH, error_for, install_error_handlers
from cellarch.common.headers import INTERNAL_TOKEN_HEADER, REQUEST_ID_HEADER, TENANT_HEADER
from cellarch.common.logging import configure_logging
from cellarch.common.security import TokenAuth
from cellarch.common.sessions import (
    SESSION_COOKIE,
    InvalidSessionError,
    SessionVerifier,
    tenant_from_host,
)
from cellarch.data_plane.router.metering import Metering
from cellarch.data_plane.router.rate_limit import Decision, RateLimiter, build_rate_limiter
from cellarch.data_plane.router.routing import (
    RoutingCache,
    RoutingUnavailableError,
    TenantNotFoundError,
    TenantResolver,
)

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
_DROP_REQUEST = HOP_BY_HOP | {"host", "content-length", INTERNAL_TOKEN_HEADER.lower()}
# The UI's credentials and claimed tenant stay at the router; the cell gets X-Tenant-ID.
_DROP_UI_REQUEST = {"cookie", "authorization", TENANT_HEADER.lower()}
_DROP_RESPONSE = HOP_BY_HOP | {"content-length", "content-encoding"}
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
RETRY_AFTER = {"Retry-After": "5"}


@dataclass(frozen=True)
class RouterSettings:
    control_plane_url: str
    router_token: str
    cache_ttl_seconds: float = 5.0
    request_timeout_seconds: float = 10.0
    # Number of router replicas sharing the traffic; each enforces limit / replicas.
    # Locally a setting; on AWS read from the running router replicas (stage 6).
    router_replicas: int = 1
    # Guards /_router/stats (it lists tenant ids). Without it, stats are switched off.
    metrics_token: str | None = None
    # memory (default) or redis: one shared bucket per tenant across all routers.
    rate_limit_backend: str = "memory"
    redis_url: str | None = None
    redis_timeout_seconds: float = 0.05  # keep Redis from slowing requests when it's down
    rate_limit_key_prefix: str = "cellarch:rl:"
    # Web UI sessions (docs/plan.md §2.4): /graphql is off without a JWKS URL.
    session_jwks_url: str | None = None
    session_issuer: str = ""
    session_audience: str = "cellarch-web"
    tenant_base_domain: str = "localhost"  # tenants' hosts are <tenant>.<base domain>

    @classmethod
    def from_env(cls) -> "RouterSettings":
        return cls(
            control_plane_url=require_env("CONTROL_PLANE_URL"),
            router_token=require_env("ROUTER_TOKEN"),
            cache_ttl_seconds=float(os.environ.get("ROUTE_CACHE_TTL_SECONDS", "5")),
            request_timeout_seconds=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10")),
            router_replicas=int(os.environ.get("ROUTER_REPLICAS", "1")),
            metrics_token=os.environ.get("ROUTER_METRICS_TOKEN") or None,
            rate_limit_backend=os.environ.get("RATE_LIMIT_BACKEND", "memory"),
            redis_url=os.environ.get("REDIS_URL") or None,
            session_jwks_url=os.environ.get("SESSION_JWKS_URL") or None,
            session_issuer=os.environ.get("SESSION_ISSUER", ""),
            session_audience=os.environ.get("SESSION_AUDIENCE", "cellarch-web"),
            tenant_base_domain=os.environ.get("TENANT_BASE_DOMAIN", "localhost"),
        )


def _normalize(path: str) -> str:
    return posixpath.normpath("/" + path.lstrip("/"))


def is_internal_path(path: str) -> bool:
    """True for any path that resolves to /internal, however it is encoded or dotted."""
    normalized = _normalize(unquote(path))
    return normalized == "/internal" or normalized.startswith("/internal/")


def rate_limit_headers(decision: Decision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after),
    }


def forwardable_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers if k.lower() not in _DROP_REQUEST]


def create_app(
    settings: RouterSettings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    clock: Clock = monotonic,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or RouterSettings.from_env()
    configure_logging("router")
    http = http_client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
    cache = RoutingCache(settings.cache_ttl_seconds, clock=clock)
    resolver = TenantResolver(http, settings.control_plane_url, settings.router_token, cache)
    limiter = rate_limiter or build_rate_limiter(settings, clock)
    metering = Metering()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if http_client is None:
            await http.aclose()

    app = FastAPI(title="Cell router", lifespan=lifespan, openapi_url=None, docs_url=None)
    install_error_handlers(app)

    @app.get("/_router/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if settings.metrics_token:
        metrics_auth = TokenAuth({"metrics": settings.metrics_token})

        @app.get("/_router/stats", dependencies=[metrics_auth.require("metrics")])
        def stats() -> dict[str, Any]:
            return {
                "since": metering.since,
                "router_replicas": settings.router_replicas,
                "tenants": metering.snapshot(),
            }

    async def forward(
        entry: RoutingEntry,
        request: Request,
        path: str,
        body: bytes,
        headers: list[tuple[str, str]],
    ) -> httpx.Response:
        url = f"{entry.cell_url.rstrip('/')}{quote(path)}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        return await http.request(request.method, url, content=body, headers=headers)

    verifier: SessionVerifier | None = None
    if settings.session_jwks_url:
        jwks_url = settings.session_jwks_url

        async def fetch_jwks() -> dict[str, Any]:
            response = await http.get(jwks_url)
            response.raise_for_status()
            document: dict[str, Any] = response.json()
            return document

        verifier = SessionVerifier(
            issuer=settings.session_issuer,
            audience=settings.session_audience,
            fetch_jwks=fetch_jwks,
            clock=clock,
        )

    async def ui_tenant(request: Request) -> str | Response:
        """The tenant of a web UI request: from the host, proven by the session."""
        fail = partial(error_for, GRAPHQL_PATH)
        if verifier is None:
            return fail(404, "not_found", "Not Found")
        tenant = tenant_from_host(request.headers.get("host"), settings.tenant_base_domain)
        if tenant is None:
            return fail(
                400,
                "tenant_host_required",
                f"use your tenant's own address, e.g. acme.{settings.tenant_base_domain}",
            )
        token = request.cookies.get(SESSION_COOKIE)
        authorization = request.headers.get("authorization", "")
        if not token and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        try:
            session_tenant = await verifier.tenant(token)
        except InvalidSessionError:
            return fail(401, "unauthenticated", "sign in to continue")
        if session_tenant != tenant:
            return fail(403, "session_tenant_mismatch", "this session is for another tenant")
        return tenant

    @app.api_route("/{path:path}", methods=_METHODS, include_in_schema=False)
    async def proxy(request: Request) -> Response:
        raw_path: str = request.scope["path"]
        path_key = _normalize(unquote(raw_path))
        fail = partial(error_for, path_key)
        if is_internal_path(raw_path) or path_key.startswith("/_router/"):
            # Cells' /internal API is never exposed; /_router/* is the router's own space.
            return fail(404, "not_found", "Not Found")

        ui = path_key == GRAPHQL_PATH
        if ui:
            resolved = await ui_tenant(request)
            if isinstance(resolved, Response):
                return resolved
            tenant_id: str | None = resolved
        else:
            tenant_id = request.headers.get(TENANT_HEADER)
        if not tenant_id:
            return fail(400, "missing_tenant", f"{TENANT_HEADER} header is required")
        if not is_valid_tenant_id(tenant_id):
            return fail(400, "invalid_tenant", "malformed tenant id")

        try:
            entry = await resolver.resolve(tenant_id)
        except TenantNotFoundError:
            return fail(404, "unknown_tenant", "tenant not found")
        except RoutingUnavailableError:
            return fail(503, "routing_unavailable", "routing unavailable", RETRY_AFTER)

        # Only tenants the control plane knows are metered, so made-up ids can't grow it.
        response, throttled = await serve(request, raw_path, tenant_id, entry, ui=ui)
        metering.record(tenant_id, response.status_code, throttled=throttled)
        return response

    async def serve(
        request: Request, raw_path: str, tenant_id: str, entry: RoutingEntry, *, ui: bool
    ) -> tuple[Response, bool]:
        """Limit and forward a request for a known tenant; returns (response, throttled)."""
        fail = partial(error_for, _normalize(unquote(raw_path)))
        if entry.status == "migrating" and request.method not in _READ_METHODS:
            # Being moved to another cell: reads go on, writes wait (docs/plan.md §5).
            migrating = fail(
                503, "tenant_migrating", "the tenant is being moved; retry shortly", RETRY_AFTER
            )
            return migrating, False
        if entry.status not in ("active", "migrating"):
            return (
                fail(503, "tenant_unavailable", "tenant unavailable", RETRY_AFTER),
                False,
            )

        decision = limiter.check(tenant_id, entry.rate_limit) if entry.rate_limit else None
        if decision is not None and not decision.allowed:
            limited = {**rate_limit_headers(decision), "Retry-After": str(decision.retry_after)}
            return fail(429, "rate_limited", "tenant rate limit exceeded", limited), True

        body = await request.body()
        path = _normalize(raw_path)
        headers = forwardable_headers(request.headers.items())
        if ui:
            headers = [(k, v) for k, v in headers if k.lower() not in _DROP_UI_REQUEST]
            headers.append((TENANT_HEADER, tenant_id))
        if REQUEST_ID_HEADER.lower() not in request.headers:
            headers.append((REQUEST_ID_HEADER, uuid.uuid4().hex))

        try:
            upstream = await forward(entry, request, path, body, headers)
            if upstream.status_code == 421:
                # The cell no longer hosts this tenant: the cached route is stale.
                try:
                    fresh = await resolver.resolve(tenant_id, refresh=True)
                except TenantNotFoundError:
                    return fail(404, "unknown_tenant", "tenant not found"), False
                except RoutingUnavailableError:  # pragma: no cover - cached entry exists here
                    fresh = entry
                if fresh.cell_id == entry.cell_id or fresh.status != "active":
                    stale = fail(503, "routing_stale", "route is stale", RETRY_AFTER)
                    return stale, False
                upstream = await forward(fresh, request, path, body, headers)
        except httpx.HTTPError:
            unreachable = fail(502, "cell_unreachable", f"cell {entry.cell_id} is unreachable")
            return unreachable, False

        response = Response(content=upstream.content, status_code=upstream.status_code)
        for key, value in upstream.headers.multi_items():
            if key.lower() not in _DROP_RESPONSE:
                response.headers.append(key, value)
        if decision is not None:
            response.headers.update(rate_limit_headers(decision))
        return response, False

    return app
