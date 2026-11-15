from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.contracts import RoutingEntry
from cellarch.common.headers import INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.data_plane.router.app import (
    RouterSettings,
    create_app,
    forwardable_headers,
    is_internal_path,
)
from cellarch.data_plane.router.routing import (
    RoutingCache,
    RoutingUnavailableError,
    TenantNotFoundError,
    TenantResolver,
)
from tests.fakes import async_client


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def entry(cell_id: str = "cell-1", status: str = "active") -> RoutingEntry:
    return RoutingEntry.model_validate(
        {"tenant_id": "acme", "cell_id": cell_id, "cell_url": f"http://{cell_id}", "status": status}
    )


class TestRoutingCache:
    def test_fresh_then_stale(self) -> None:
        clock = FakeClock()
        cache = RoutingCache(ttl_seconds=5, clock=clock)
        cache.put(entry())
        cached = cache.get("acme")
        assert cached is not None and cached.fresh
        clock.now = 5.1
        stale = cache.get("acme")
        assert stale is not None and not stale.fresh
        assert stale.entry == entry()

    def test_put_refreshes_and_invalidate_removes(self) -> None:
        clock = FakeClock()
        cache = RoutingCache(ttl_seconds=5, clock=clock)
        cache.put(entry())
        clock.now = 10
        cache.put(entry("cell-2"))
        cached = cache.get("acme")
        assert cached is not None and cached.fresh and cached.entry.cell_id == "cell-2"
        cache.invalidate("acme")
        cache.invalidate("acme")
        assert cache.get("acme") is None

    def test_ttl_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="ttl"):
            RoutingCache(ttl_seconds=0)


Handler = Callable[[httpx.Request], httpx.Response]


def resolver(handler: Handler, clock: FakeClock | None = None) -> tuple[TenantResolver, list[str]]:
    calls: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers[INTERNAL_TOKEN_HEADER] == "router-t"
        return handler(request)

    http = async_client(transport=httpx.MockTransport(recording))
    cache = RoutingCache(ttl_seconds=5, clock=clock or FakeClock())
    return TenantResolver(http, "http://cp", "router-t", cache), calls


def ok(cell_id: str = "cell-1") -> Handler:
    return lambda r: httpx.Response(200, json=entry(cell_id).model_dump())


class TestTenantResolver:
    def test_caches_lookups(self) -> None:
        res, calls = resolver(ok())
        assert asyncio.run(res.resolve("acme")) == entry()
        assert asyncio.run(res.resolve("acme")) == entry()
        assert calls == ["/v1/routing/tenants/acme"]

    def test_refresh_bypasses_fresh_cache(self) -> None:
        res, calls = resolver(ok())
        asyncio.run(res.resolve("acme"))
        asyncio.run(res.resolve("acme", refresh=True))
        assert len(calls) == 2

    def test_unknown_tenant_raises_and_clears_cache(self) -> None:
        clock = FakeClock()
        state = {"known": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if state["known"]:
                return httpx.Response(200, json=entry().model_dump())
            return httpx.Response(404, json={"error": "tenant_not_found"})

        res, _ = resolver(handler, clock)
        asyncio.run(res.resolve("acme"))
        state["known"] = False
        clock.now = 60
        with pytest.raises(TenantNotFoundError):
            asyncio.run(res.resolve("acme"))
        with pytest.raises(TenantNotFoundError):
            asyncio.run(res.resolve("acme"))

    @pytest.mark.parametrize(
        "failure",
        [
            lambda r: httpx.Response(500),
            lambda r: httpx.Response(200, content=b"not json"),
            lambda r: httpx.Response(200, json={"unexpected": True}),
        ],
    )
    def test_control_plane_failure_serves_stale_route(self, failure: Handler) -> None:
        clock = FakeClock()
        state = {"up": True}

        def handler(request: httpx.Request) -> httpx.Response:
            return ok()(request) if state["up"] else failure(request)

        res, _ = resolver(handler, clock)
        asyncio.run(res.resolve("acme"))
        state["up"] = False
        clock.now = 60
        assert asyncio.run(res.resolve("acme")) == entry()

    def test_connection_error_without_cache_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        res, _ = resolver(handler)
        with pytest.raises(RoutingUnavailableError):
            asyncio.run(res.resolve("acme"))


class TestHelpers:
    @pytest.mark.parametrize(
        "path",
        ["/internal", "/internal/", "/internal/tenants", "/v1/../internal/x", "//internal/x"],
    )
    def test_internal_paths(self, path: str) -> None:
        assert is_internal_path(path)

    @pytest.mark.parametrize("path", ["/", "/v1/items", "/v1/internal-notes", "/internalx"])
    def test_public_paths(self, path: str) -> None:
        assert not is_internal_path(path)

    def test_forwardable_headers_drop_hop_by_hop_and_internal(self) -> None:
        headers = forwardable_headers(
            [
                ("host", "router"),
                ("connection", "keep-alive"),
                ("content-length", "3"),
                (INTERNAL_TOKEN_HEADER.lower(), "secret"),
                ("x-tenant-id", "acme"),
                ("accept", "application/json"),
            ]
        )
        assert headers == [("x-tenant-id", "acme"), ("accept", "application/json")]


def router_client(cp: Handler, cell: Handler) -> TestClient:
    def dispatch(request: httpx.Request) -> httpx.Response:
        return cp(request) if request.url.host == "cp" else cell(request)

    app = create_app(
        RouterSettings(control_plane_url="http://cp", router_token="router-t"),
        http_client=async_client(transport=httpx.MockTransport(dispatch)),
    )
    return TestClient(app)


class TestRouterApp:
    def test_healthz(self) -> None:
        client = router_client(ok(), lambda r: httpx.Response(200))
        assert client.get("/_router/healthz").json() == {"status": "ok"}

    def test_forwards_method_path_query_body_and_filters_response_headers(self) -> None:
        seen: list[httpx.Request] = []

        def cell(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                201,
                json={"echo": json.loads(request.content)},
                headers={"x-cell-id": "cell-1", "connection": "close"},
            )

        client = router_client(ok(), cell)
        response = client.post(
            "/v1/items?limit=5", json={"name": "a"}, headers={TENANT_HEADER: "acme"}
        )
        assert response.status_code == 201
        assert response.json() == {"echo": {"name": "a"}}
        assert response.headers["x-cell-id"] == "cell-1"
        assert "connection" not in response.headers
        assert str(seen[0].url) == "http://cell-1/v1/items?limit=5"
        assert seen[0].headers["x-request-id"]

    def test_keeps_client_request_id(self) -> None:
        seen: list[httpx.Request] = []

        def cell(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        client = router_client(ok(), cell)
        client.get("/v1/items", headers={TENANT_HEADER: "acme", "x-request-id": "req-1"})
        assert seen[0].headers["x-request-id"] == "req-1"

    def test_tenant_not_active_is_unavailable(self) -> None:
        cp = lambda r: httpx.Response(200, json=entry(status="provisioning").model_dump())  # noqa: E731
        response = router_client(cp, lambda r: httpx.Response(200)).get(
            "/v1/items", headers={TENANT_HEADER: "acme"}
        )
        assert (response.status_code, response.json()["error"]) == (503, "tenant_unavailable")
        assert response.headers["retry-after"]

    def test_a_migrating_tenant_can_read_but_not_write(self) -> None:
        cp = lambda r: httpx.Response(200, json=entry(status="migrating").model_dump())  # noqa: E731
        client = router_client(cp, lambda r: httpx.Response(200, json={"items": []}))
        for method in ("GET", "HEAD"):
            assert (
                client.request(method, "/v1/users", headers={TENANT_HEADER: "acme"}).status_code
                == 200
            )
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = client.request(method, "/v1/users", headers={TENANT_HEADER: "acme"})
            assert (response.status_code, response.json()["error"]) == (503, "tenant_migrating")
            assert response.headers["retry-after"]

    def test_misdirected_twice_to_same_cell_is_stale_routing(self) -> None:
        cell = lambda r: httpx.Response(421, json={"error": "tenant_not_in_cell"})  # noqa: E731
        response = router_client(ok(), cell).get("/v1/items", headers={TENANT_HEADER: "acme"})
        assert (response.status_code, response.json()["error"]) == (503, "routing_stale")

    def test_tenant_deleted_after_stale_route_is_unknown(self) -> None:
        calls = {"n": 0}

        def cp(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json=entry().model_dump())
            return httpx.Response(404, json={"error": "tenant_not_found"})

        cell = lambda r: httpx.Response(421, json={"error": "tenant_not_in_cell"})  # noqa: E731
        response = router_client(cp, cell).get("/v1/items", headers={TENANT_HEADER: "acme"})
        assert (response.status_code, response.json()["error"]) == (404, "unknown_tenant")

    def test_owned_http_client_is_closed_on_shutdown(self) -> None:
        app = create_app(RouterSettings(control_plane_url="http://cp", router_token="r"))
        with TestClient(app) as client:
            assert client.get("/_router/healthz").status_code == 200

    def test_settings_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp:8000")
        monkeypatch.setenv("ROUTER_TOKEN", "r")
        monkeypatch.setenv("ROUTE_CACHE_TTL_SECONDS", "2.5")
        settings = RouterSettings.from_env()
        assert (settings.control_plane_url, settings.cache_ttl_seconds) == ("http://cp:8000", 2.5)


class TestWebUiSessions:
    """The router's /graphql: the tenant from the host, proven by a session token."""

    def test_without_session_settings_there_is_no_graphql(self) -> None:
        client = router_client(ok(), lambda r: httpx.Response(200))
        response = client.get("/graphql", headers={"host": "acme.localhost"})
        assert (response.status_code, response.json()["errors"][0]["extensions"]["code"]) == (
            404,
            "not_found",
        )

    def test_a_bearer_token_works_like_the_cookie(self) -> None:
        from cellarch.dev_idp.app import DevIdpSettings, issue_token
        from cellarch.dev_idp.app import create_app as create_idp

        idp = create_idp(DevIdpSettings(issuer="http://idp", cookie_secure=False))
        idp_client = TestClient(idp)
        seen: list[httpx.Request] = []

        def dispatch(request: httpx.Request) -> httpx.Response:
            if request.url.host == "idp":
                return httpx.Response(200, json=idp_client.get(request.url.path).json())
            if request.url.host == "cp":
                return ok()(request)
            seen.append(request)
            return httpx.Response(200, json={"data": {}})

        app = create_app(
            RouterSettings(
                control_plane_url="http://cp",
                router_token="router-t",
                session_jwks_url="http://idp/auth/.well-known/jwks.json",
                session_issuer="http://idp",
            ),
            http_client=async_client(transport=httpx.MockTransport(dispatch)),
        )
        response = TestClient(app).get(
            "/graphql",
            headers={
                "host": "acme.localhost",
                "authorization": f"Bearer {issue_token(idp, 'acme')}",
                TENANT_HEADER: "globex",
            },
        )
        assert response.status_code == 200, response.text
        assert seen[0].headers[TENANT_HEADER] == "acme"
        assert "authorization" not in seen[0].headers  # the token stays at the router

    def test_settings_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp")
        monkeypatch.setenv("ROUTER_TOKEN", "t")
        monkeypatch.setenv("SESSION_JWKS_URL", "http://idp/jwks")
        monkeypatch.setenv("TENANT_BASE_DOMAIN", "platform.test")
        settings = RouterSettings.from_env()
        assert (settings.session_jwks_url, settings.tenant_base_domain) == (
            "http://idp/jwks",
            "platform.test",
        )
