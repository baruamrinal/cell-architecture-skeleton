"""Router: resolves the tenant's cell and forwards the request there.

Stateless apart from the route cache. Never exposes cells' /internal endpoints and
never forwards internal credentials from clients.
"""

import os
import posixpath
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote, unquote

import httpx
from fastapi import FastAPI, Request, Response

from cellarch.common.clock import Clock, monotonic
from cellarch.common.config import require_env
from cellarch.common.contracts import RoutingEntry, is_valid_tenant_id
from cellarch.common.errors import error_response, install_error_handlers
from cellarch.common.headers import INTERNAL_TOKEN_HEADER, REQUEST_ID_HEADER, TENANT_HEADER
from cellarch.common.logging import configure_logging
from cellarch.data_plane.router.rate_limit import Decision, InMemoryRateLimiter, RateLimiter
from cellarch.data_plane.router.routing import (
    RoutingCache,
    RoutingUnavailableError,
    TenantNotFoundError,
    TenantResolver,
)

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
    # Locally a setting; on AWS read from the running ECS task count (stage 5).
    router_replicas: int = 1

    @classmethod
    def from_env(cls) -> "RouterSettings":
        return cls(
            control_plane_url=require_env("CONTROL_PLANE_URL"),
            router_token=require_env("ROUTER_TOKEN"),
            cache_ttl_seconds=float(os.environ.get("ROUTE_CACHE_TTL_SECONDS", "5")),
            request_timeout_seconds=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10")),
            router_replicas=int(os.environ.get("ROUTER_REPLICAS", "1")),
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
    limiter = rate_limiter or InMemoryRateLimiter(clock=clock, replicas=settings.router_replicas)

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

    @app.api_route("/{path:path}", methods=_METHODS, include_in_schema=False)
    async def proxy(request: Request) -> Response:
        raw_path: str = request.scope["path"]
        if is_internal_path(raw_path):
            return error_response(404, "not_found", "Not Found")

        tenant_id = request.headers.get(TENANT_HEADER)
        if not tenant_id:
            return error_response(400, "missing_tenant", f"{TENANT_HEADER} header is required")
        if not is_valid_tenant_id(tenant_id):
            return error_response(400, "invalid_tenant", "malformed tenant id")

        try:
            entry = await resolver.resolve(tenant_id)
        except TenantNotFoundError:
            return error_response(404, "unknown_tenant", "tenant not found")
        except RoutingUnavailableError:
            return error_response(503, "routing_unavailable", "routing unavailable", RETRY_AFTER)
        if entry.status != "active":
            return error_response(503, "tenant_unavailable", "tenant unavailable", RETRY_AFTER)

        decision = limiter.check(tenant_id, entry.rate_limit) if entry.rate_limit else None
        if decision is not None and not decision.allowed:
            return error_response(
                429,
                "rate_limited",
                "tenant rate limit exceeded",
                {**rate_limit_headers(decision), "Retry-After": str(decision.retry_after)},
            )

        body = await request.body()
        path = _normalize(raw_path)
        headers = forwardable_headers(request.headers.items())
        if REQUEST_ID_HEADER.lower() not in request.headers:
            headers.append((REQUEST_ID_HEADER, uuid.uuid4().hex))

        try:
            upstream = await forward(entry, request, path, body, headers)
            if upstream.status_code == 421:
                # The cell no longer hosts this tenant: the cached route is stale.
                try:
                    fresh = await resolver.resolve(tenant_id, refresh=True)
                except TenantNotFoundError:
                    return error_response(404, "unknown_tenant", "tenant not found")
                except RoutingUnavailableError:  # pragma: no cover - cached entry exists here
                    fresh = entry
                if fresh.cell_id == entry.cell_id or fresh.status != "active":
                    return error_response(503, "routing_stale", "route is stale", RETRY_AFTER)
                upstream = await forward(fresh, request, path, body, headers)
        except httpx.HTTPError:
            return error_response(502, "cell_unreachable", f"cell {entry.cell_id} is unreachable")

        response = Response(content=upstream.content, status_code=upstream.status_code)
        for key, value in upstream.headers.multi_items():
            if key.lower() not in _DROP_RESPONSE:
                response.headers.append(key, value)
        if decision is not None:
            response.headers.update(rate_limit_headers(decision))
        return response

    return app
