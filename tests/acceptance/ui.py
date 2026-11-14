"""GraphQL helpers for the tenant web UI tests (the calls web-ui makes, through the router).

Every tenant has its own host (``acme.localhost``), so each call carries the session's
tenant host, as a browser on that tenant's URL does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from cellarch.common.sessions import SESSION_COOKIE
from cellarch.mgr.app import CSRF_HEADER

from .conftest import Platform

USERS = """
query Users($first: Int, $after: String) {
  users(first: $first, after: $after) {
    items { id email name orders(first: 5) { id totalCents } }
    nextCursor
  }
}"""
ME = "query { me { tenantId cellId } }"
USER = "query User($id: ID!) { user(id: $id) { id email } }"
ORDER = "query Order($id: ID!) { order(id: $id) { id userId } }"
CREATE_USER = """
mutation CreateUser($email: String!, $name: String!, $key: String) {
  createUser(input: {email: $email, name: $name}, idempotencyKey: $key) { id email name }
}"""
CREATE_ORDER = """
mutation CreateOrder($userId: ID!, $key: String) {
  createOrder(
    input: {userId: $userId, lines: [{sku: "book-1", quantity: 2, unitPriceCents: 1250}]}
    idempotencyKey: $key
  ) { id userId totalCents status }
}"""
CANCEL_ORDER = "mutation Cancel($id: ID!) { cancelOrder(id: $id) { id status } }"
DELETE_USER = "mutation Delete($id: ID!) { deleteUser(id: $id) }"
BASE_DOMAIN = "localhost"


@dataclass(frozen=True)
class UiSession:
    """A browser on ``<tenant>.localhost`` holding (or not) a session token."""

    tenant: str
    token: str | None

    @property
    def host(self) -> str:
        return f"{self.tenant}.{BASE_DOMAIN}"


def sign_in(platform: Platform, tenant_id: str) -> UiSession:
    """The dev identity provider's sign-in on the tenant's own host."""
    response = platform.idp.post(
        "/auth/dev/sign-in", headers={"host": f"{tenant_id}.{BASE_DOMAIN}"}
    )
    assert response.status_code == 200, response.text
    return UiSession(tenant_id, response.cookies[SESSION_COOKIE])


def query(
    platform: Platform,
    session: UiSession,
    document: str,
    variables: dict[str, Any] | None = None,
    *,
    via: Any = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """A GraphQL query, sent as GET as web-ui sends it."""
    client = via or platform.router
    params = {"query": document}
    if variables:
        params["variables"] = json.dumps(variables)
    cookies = {SESSION_COOKIE: session.token} if session.token else {}
    response: httpx.Response = client.get(
        "/graphql",
        params=params,
        cookies=cookies,
        headers={"host": session.host, **(headers or {})},
    )
    return response


def mutate(
    platform: Platform,
    session: UiSession,
    document: str,
    variables: dict[str, Any] | None = None,
    *,
    csrf: bool = True,
) -> httpx.Response:
    """A GraphQL mutation, sent as POST with the anti-CSRF header."""
    headers = {"host": session.host, **({CSRF_HEADER: "1"} if csrf else {})}
    cookies = {SESSION_COOKIE: session.token} if session.token else {}
    response: httpx.Response = platform.router.post(
        "/graphql",
        json={"query": document, "variables": variables or {}},
        cookies=cookies,
        headers=headers,
    )
    return response


def data(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    body = response.json()
    assert "errors" not in body, body
    return body["data"]


def error(response: httpx.Response) -> dict[str, Any]:
    """The first GraphQL error's extensions."""
    body = response.json()
    assert body.get("errors"), body
    extensions: dict[str, Any] = body["errors"][0].get("extensions", {})
    return extensions
