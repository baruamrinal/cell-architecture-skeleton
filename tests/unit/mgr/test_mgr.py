"""mgr against a scripted cell gateway: request checks, errors and loaders.

The whole path (router, gateway, mgr, tenant services) runs in
tests/acceptance/test_tenant_ui_backend.py; these cover the edges.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.headers import INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.mgr.app import CSRF_HEADER, MgrSettings, create_app

TOKEN = "gateway-to-mgr"
FROM_GATEWAY = {INTERNAL_TOKEN_HEADER: TOKEN, TENANT_HEADER: "acme"}
USER = {"id": "usr_1", "email": "a@x.test", "name": "A", "created_at": "t"}


def order(order_id: str, user_id: str = "usr_1") -> dict[str, Any]:
    return {
        "id": order_id,
        "user_id": user_id,
        "status": "placed",
        "lines": [{"sku": "a", "quantity": 1, "unit_price_cents": 5}],
        "total_cents": 5,
        "created_at": "t",
        "updated_at": "t",
    }


class Gateway:
    """The cell gateway's /v1 API, scripted."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, httpx.Response | Exception] = {}
        self.orders = [order("ord_1")]
        self.orders_truncated = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        answer = self.answers.get(key)
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if key == "GET /v1/orders":
            many = len(request.url.params.get_list("user_id")) > 1
            next_cursor = "c" if many and self.orders_truncated else None
            return httpx.Response(200, json={"items": self.orders, "next_cursor": next_cursor})
        if key.startswith("GET /v1/users/"):
            return httpx.Response(200, json=USER)
        if key == "GET /v1/orders/ord_1":
            return httpx.Response(200, json=order("ord_1"))
        return httpx.Response(404, json={"error": "not_found", "message": "no"})


@pytest.fixture
def gateway() -> Gateway:
    return Gateway()


def client(gateway: Gateway, **settings: Any) -> TestClient:
    app = create_app(
        MgrSettings(cell_id="cell-1", gateway_url="http://gw", gateway_token=TOKEN, **settings),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(gateway.handler)),
    )
    return TestClient(app)


def get(c: TestClient, query: str, variables: Any = None, **params: str) -> httpx.Response:
    q = {"query": query, **params}
    if variables is not None:
        q["variables"] = variables if isinstance(variables, str) else json.dumps(variables)
    response: httpx.Response = c.get("/graphql", params=q, headers=FROM_GATEWAY)
    return response


def code(response: httpx.Response) -> str:
    result: str = response.json()["errors"][0]["extensions"]["code"]
    return result


class TestRequests:
    def test_health_and_readiness(self, gateway: Gateway) -> None:
        c = client(gateway)
        assert c.get("/healthz").json()["service"] == "mgr"
        assert c.get("/readyz").status_code == 200
        gateway.answers["GET /healthz"] = httpx.ConnectError("down")
        assert c.get("/readyz").status_code == 503

    def test_settings_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in {
            "CELL_ID": "cell-7",
            "GATEWAY_URL": "http://gw",
            "MGR_TOKEN": "secret-mgr-token",
            "GRAPHQL_INTROSPECTION": "true",
        }.items():
            monkeypatch.setenv(name, value)
        settings = MgrSettings.from_env()
        assert (settings.cell_id, settings.introspection, settings.max_depth) == ("cell-7", True, 8)
        assert "secret-mgr-token" not in repr(settings)

    def test_needs_a_valid_tenant(self, gateway: Gateway) -> None:
        response = client(gateway).get(
            "/graphql",
            params={"query": "{ me { tenantId } }"},
            headers={INTERNAL_TOKEN_HEADER: TOKEN},
        )
        assert (response.status_code, code(response)) == (400, "invalid_tenant")

    @pytest.mark.parametrize(
        ("kwargs", "status", "error"),
        [
            (
                {"content": b"{}", "headers": {"content-type": "text/plain"}},
                415,
                "unsupported_media_type",
            ),
            (
                {"content": b"not json", "headers": {"content-type": "application/json"}},
                400,
                "invalid_query",
            ),
            ({"json": [1, 2]}, 400, "invalid_query"),
            ({"json": {"query": ""}}, 400, "invalid_query"),
            ({"json": {"query": "{ me { tenantId } }", "variables": [1]}}, 400, "invalid_query"),
        ],
    )
    def test_malformed_posts(
        self, gateway: Gateway, kwargs: dict[str, Any], status: int, error: str
    ) -> None:
        headers = {**FROM_GATEWAY, CSRF_HEADER: "1", **kwargs.pop("headers", {})}
        response = client(gateway).post("/graphql", headers=headers, **kwargs)
        assert (response.status_code, code(response)) == (status, error)

    def test_malformed_gets(self, gateway: Gateway) -> None:
        c = client(gateway)
        assert code(get(c, "{ me { tenantId } }", "not json")) == "invalid_query"
        assert code(get(c, "{ me {")) == "invalid_query"
        assert (
            code(get(c, "query A { me { tenantId } } query B { me { cellId } }")) == "invalid_query"
        )
        assert code(get(c, "subscription { me }")) == "invalid_query"

    def test_operation_name_picks_one(self, gateway: Gateway) -> None:
        doc = "query A { me { tenantId } } query B { me { cellId } }"
        response = get(client(gateway), doc, operationName="B")
        assert response.json()["data"] == {"me": {"cellId": "cell-1"}}

    def test_fragments_count_towards_depth_and_cost(self, gateway: Gateway) -> None:
        c = client(gateway, max_depth=2)
        doc = "query { ...F } fragment F on Query { users { items { ... on User { id } } } }"
        assert code(get(c, doc)) == "query_too_deep"
        assert code(get(c, "query { ...Missing }")) == "invalid_query"
        looping = "query { ...F } fragment F on Query { ...F }"
        assert code(get(c, looping)) == "invalid_query"
        costly = client(gateway, max_cost=5)
        assert code(
            get(costly, "query($n: Int) { users(first: $n) { items { id } } }", {"n": 50})
        ) == ("query_too_costly")

    def test_introspection_is_off_unless_enabled(self, gateway: Gateway) -> None:
        assert (
            code(get(client(gateway), "{ __schema { types { name } } }"))
            == "introspection_disabled"
        )
        allowed = get(client(gateway, introspection=True), '{ __type(name: "User") { name } }')
        assert allowed.json()["data"] == {"__type": {"name": "User"}}


class TestResolvers:
    def test_orders_query_with_a_user_and_a_cursor(self, gateway: Gateway) -> None:
        response = get(
            client(gateway),
            'query { orders(userId: "usr_1", first: 2, after: "c") '
            "{ items { id user { email } } } }",
        )
        assert response.json()["data"]["orders"]["items"] == [
            {"id": "ord_1", "user": {"email": "a@x.test"}}
        ]
        sent = [r for r in gateway.requests if r.url.path == "/v1/orders"][0]
        assert (sent.url.params["user_id"], sent.url.params["cursor"]) == ("usr_1", "c")

    def test_order_by_id(self, gateway: Gateway) -> None:
        found = get(client(gateway), 'query { order(id: "ord_1") { id } }')
        assert found.json()["data"] == {"order": {"id": "ord_1"}}

    def test_out_of_range_page_sizes_are_refused(self, gateway: Gateway) -> None:
        response = get(client(gateway), "query { users(first: 0) { items { id } } }")
        assert code(response) == "invalid_argument"

    def test_a_truncated_batch_falls_back_per_user(self, gateway: Gateway) -> None:
        gateway.answers["GET /v1/users"] = httpx.Response(
            200, json={"items": [USER, {**USER, "id": "usr_2"}], "next_cursor": None}
        )
        gateway.orders_truncated = True
        response = get(client(gateway), "query { users { items { id orders(first: 3) { id } } } }")
        assert response.status_code == 200, response.text
        order_calls = [r for r in gateway.requests if r.url.path == "/v1/orders"]
        assert len(order_calls) == 3  # one batch, then one per user that may be missing some

    def test_gateway_errors_become_graphql_errors(self, gateway: Gateway) -> None:
        gateway.answers["POST /v1/users"] = httpx.Response(
            503,
            json={"error": "tenant_migrating", "message": "moving"},
            headers={"Retry-After": "2"},
        )
        response = client(gateway).post(
            "/graphql",
            json={"query": 'mutation { createUser(input: {email: "a@x.test", name: "A"}) { id } }'},
            headers={**FROM_GATEWAY, CSRF_HEADER: "1"},
        )
        error = response.json()["errors"][0]["extensions"]
        assert (error["code"], error["status"], error["retryAfter"]) == ("tenant_migrating", 503, 2)
        assert response.headers["Retry-After"] == "2"

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            (httpx.ConnectError("down"), "cell_unavailable"),
            (httpx.Response(500, text="oops"), "upstream_error"),
        ],
    )
    def test_an_unreachable_or_odd_gateway(
        self, gateway: Gateway, answer: Any, expected: str
    ) -> None:
        gateway.answers["GET /v1/users"] = answer
        assert code(get(client(gateway), "query { users { items { id } } }")) == expected

    def test_unexpected_failures_are_internal_errors(
        self, gateway: Gateway, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cellarch.mgr import schema

        def broken(row: dict[str, Any]) -> Any:
            raise RuntimeError("bug")

        monkeypatch.setattr(schema, "_user", broken)
        assert code(get(client(gateway), 'query { user(id: "usr_1") { id } }')) == "internal_error"
        assert code(get(client(gateway), "query { nope }")) == "invalid_query"


def test_owned_http_client_is_closed() -> None:
    app = create_app(MgrSettings(cell_id="cell-1", gateway_url="http://gw", gateway_token=TOKEN))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200


def test_a_contract_the_services_no_longer_meet_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cellarch.mgr import contract

    calls = (
        contract.Call("user-account", "get", "/v1/nowhere"),
        contract.Call("user-account", "get", "/v1/users", frozenset({"colour"}), frozenset({"x"})),
        contract.Call("order-processing", "delete", "/v1/orders"),
    )
    monkeypatch.setattr(contract, "CALLS", calls)
    assert contract.unmet() == [
        "GET /v1/nowhere: not offered by user-account",
        "GET /v1/users: no query parameter colour",
        "GET /v1/users: response has no field x",
        "DELETE /v1/orders: not offered by order-processing",
    ]


def test_first_given_as_a_non_integer_variable_uses_the_default(gateway: Gateway) -> None:
    c = client(gateway, max_cost=50)
    doc = "query($n: Int) { users(first: $n) { items { id } } }"
    assert get(c, doc, {"n": None}).status_code == 200  # default 20: within the budget


def test_arguments_other_than_first_leave_the_default(gateway: Gateway) -> None:
    gateway.answers["GET /v1/users"] = httpx.Response(200, json={"items": [], "next_cursor": None})
    c = client(gateway, max_cost=50)
    assert get(c, 'query { users(after: "c", first: "x") { items { id } } }').status_code == 200
    plain = get(c, "query { orders { items { id } } }")
    assert plain.json()["data"] == {"orders": {"items": [{"id": "ord_1"}]}}
    sent = [r for r in gateway.requests if r.url.path == "/v1/orders"][-1]
    assert "user_id" not in sent.url.params and "cursor" not in sent.url.params


def test_an_injected_http_client_is_left_open(gateway: Gateway) -> None:
    with client(gateway) as c:
        assert c.get("/healthz").status_code == 200


def test_inline_response_schemas_are_read_too() -> None:
    from cellarch.mgr.contract import _fields

    assert _fields({}, {"properties": {"a": {}, "b": {}}}) == {"a", "b"}
