"""Stage 2.5 acceptance tests: one tenant's service stack (A42-A49).

user-account and order-processing run in-process as the tenant's own services,
sharing the tenant's database, with order-processing calling user-account over
HTTP exactly as it does between containers. Every test runs on SQLite and, when
``POSTGRES_URL`` is set, on PostgreSQL (tests/conftest.py).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.headers import INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.common.sql import DatabaseError, open_database
from cellarch.services.common.settings import ServiceSettings
from cellarch.services.order_processing.app import OrderSettings
from cellarch.services.order_processing.app import create_app as create_order_processing
from cellarch.services.user_account.app import create_app as create_user_account
from tests.fakes import async_client

from .conftest import Switch

STACK_TOKEN = "test-stack-token-acme"
USER_ACCOUNT_URL = "http://user-account"


@dataclass
class Stack:
    users: TestClient
    orders: TestClient
    user_account: Switch
    database_url: str

    def create_user(self, email: str = "ana@acme.test", **headers: str) -> httpx.Response:
        response: httpx.Response = self.users.post(
            "/v1/users", json={"email": email, "name": "Ana"}, headers=headers
        )
        return response

    def place_order(self, user_id: str, **headers: str) -> httpx.Response:
        body = {
            "user_id": user_id,
            "lines": [
                {"sku": "book-1", "quantity": 2, "unit_price_cents": 1250},
                {"sku": "pen-7", "quantity": 1, "unit_price_cents": 199},
            ],
        }
        response: httpx.Response = self.orders.post("/v1/orders", json=body, headers=headers)
        return response


@pytest.fixture
def stack(database_url: str) -> Iterator[Stack]:
    # The tenant's one database, shared by both services.
    common = {"tenant_id": "acme", "stack_token": STACK_TOKEN, "database_url": database_url}
    user_account = Switch(create_user_account(ServiceSettings(**common)))
    order_processing = create_order_processing(
        OrderSettings(**common, user_account_url=USER_ACCOUNT_URL),
        http_client=async_client(mounts={USER_ACCOUNT_URL: httpx.ASGITransport(app=user_account)}),
    )
    headers = {INTERNAL_TOKEN_HEADER: STACK_TOKEN, TENANT_HEADER: "acme"}
    with (
        TestClient(user_account, base_url=USER_ACCOUNT_URL, headers=headers) as users,
        TestClient(order_processing, base_url="http://order-processing", headers=headers) as orders,
    ):
        yield Stack(users, orders, user_account, database_url)


def test_a42_a_user_places_an_order_and_cancels_it_once(stack: Stack) -> None:
    user = stack.create_user().json()

    placed = stack.place_order(user["id"])
    fetched = stack.orders.get(f"/v1/orders/{placed.json()['id']}")
    cancelled = stack.orders.post(f"/v1/orders/{placed.json()['id']}/cancel")
    again = stack.orders.post(f"/v1/orders/{placed.json()['id']}/cancel")

    assert placed.status_code == 201
    order = placed.json()
    assert (order["status"], order["user_id"], order["total_cents"]) == ("placed", user["id"], 2699)
    assert fetched.json() == order
    assert cancelled.json()["status"] == "cancelled"
    assert (again.status_code, again.json()["error"]) == (409, "order_already_cancelled")


def test_a43_orders_for_unknown_users_are_rejected(stack: Stack) -> None:
    response = stack.place_order("usr_does_not_exist")

    assert (response.status_code, response.json()["error"]) == (422, "unknown_user")
    assert stack.orders.get("/v1/orders").json()["items"] == []


def test_a44_user_account_down_blocks_new_orders_but_not_reads(stack: Stack) -> None:
    user = stack.create_user().json()
    stack.place_order(user["id"])
    stack.user_account.reachable = False

    blocked = stack.place_order(user["id"])
    listed = stack.orders.get("/v1/orders")
    ready = stack.orders.get("/readyz")

    assert (blocked.status_code, blocked.json()["error"]) == (503, "dependency_unavailable")
    assert blocked.headers["Retry-After"]
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert ready.status_code == 200


def test_a45_idempotency_keys_prevent_duplicates(stack: Stack) -> None:
    first = stack.create_user(**{"Idempotency-Key": "signup-123"})
    retry = stack.create_user(**{"Idempotency-Key": "signup-123"})
    reused = stack.create_user("other@acme.test", **{"Idempotency-Key": "signup-123"})

    user_id = first.json()["id"]
    order = stack.place_order(user_id, **{"Idempotency-Key": "checkout-9"})
    order_retry = stack.place_order(user_id, **{"Idempotency-Key": "checkout-9"})

    assert (first.status_code, retry.status_code) == (201, 201)
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replay"] == "true"
    assert "Idempotent-Replay" not in first.headers
    assert (reused.status_code, reused.json()["error"]) == (422, "idempotency_key_reused")
    assert len(stack.users.get("/v1/users").json()["items"]) == 1
    assert order_retry.json() == order.json()
    assert len(stack.orders.get("/v1/orders").json()["items"]) == 1


def test_a46_lists_are_paged_with_cursors(stack: Stack) -> None:
    emails = [f"user{n}@acme.test" for n in range(5)]
    for email in emails:
        stack.create_user(email)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = stack.users.get("/v1/users", params=params).json()
        seen.extend(u["email"] for u in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == emails  # creation order, each exactly once
    assert pages == 3
    bad = stack.users.get("/v1/users", params={"cursor": "not-a-cursor"})
    assert (bad.status_code, bad.json()["error"]) == (400, "invalid_cursor")


@pytest.mark.parametrize(
    ("headers", "status", "error"),
    [
        ({INTERNAL_TOKEN_HEADER: "wrong-token", TENANT_HEADER: "acme"}, 401, "unauthenticated"),
        ({INTERNAL_TOKEN_HEADER: STACK_TOKEN, TENANT_HEADER: "globex"}, 421, "tenant_mismatch"),
        ({INTERNAL_TOKEN_HEADER: STACK_TOKEN}, 400, "missing_tenant"),
    ],
)
def test_a47_services_accept_only_their_stack_and_tenant(
    stack: Stack, headers: dict[str, str], status: int, error: str
) -> None:
    for client, path in ((stack.users, "/v1/users"), (stack.orders, "/v1/orders")):
        client.headers.clear()
        response = client.get(path, headers=headers)
        assert (response.status_code, response.json()["error"]) == (status, error)


def test_a48_migrations_and_health_probes(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    # Starting the services again must not re-apply migrations.
    common = {"tenant_id": "acme", "stack_token": STACK_TOKEN, "database_url": stack.database_url}
    create_user_account(ServiceSettings(**common))
    with open_database(stack.database_url).connect() as conn:
        users_versions = conn.execute(
            "SELECT version FROM user_account_schema_migrations ORDER BY version"
        ).fetchall()
        orders_versions = conn.execute(
            "SELECT version FROM order_processing_schema_migrations ORDER BY version"
        ).fetchall()
    assert users_versions == [{"version": 1}]
    assert orders_versions == [{"version": 1}]

    assert stack.users.get("/healthz").json()["service"] == "user-account"
    assert stack.orders.get("/readyz").status_code == 200

    def broken_database() -> None:
        raise DatabaseError("database is unavailable")

    monkeypatch.setattr(stack.user_account.app.state.database, "ping", broken_database)  # type: ignore[attr-defined]
    not_ready = stack.users.get("/readyz")
    assert (not_ready.status_code, not_ready.json()["error"]) == (503, "not_ready")
    assert stack.users.get("/healthz").status_code == 200  # alive, just not ready


def test_a49_instances_starting_together_migrate_once(database_url: str) -> None:
    common = {"tenant_id": "acme", "stack_token": STACK_TOKEN, "database_url": database_url}
    errors: list[BaseException] = []

    def start() -> None:
        try:
            create_user_account(ServiceSettings(**common))
        except BaseException as exc:  # reported below, not lost in the thread
            errors.append(exc)

    instances = [threading.Thread(target=start) for _ in range(4)]
    for instance in instances:
        instance.start()
    for instance in instances:
        instance.join()

    assert errors == []
    with open_database(database_url).connect() as conn:
        versions = conn.execute("SELECT version FROM user_account_schema_migrations").fetchall()
    assert versions == [{"version": 1}]
