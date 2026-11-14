from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.headers import INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.common.sql import sqlite_url
from cellarch.services.common.settings import ServiceSettings
from cellarch.services.order_processing.app import OrderSettings
from cellarch.services.order_processing.app import create_app as create_orders
from cellarch.services.user_account.app import create_app as create_users
from tests.fakes import async_client

HEADERS = {INTERNAL_TOKEN_HEADER: "tok", TENANT_HEADER: "acme"}


def users_client(tmp_path: Path) -> TestClient:
    app = create_users(ServiceSettings("acme", "tok", sqlite_url(tmp_path / "acme.db")))
    return TestClient(app, headers=HEADERS)


def orders_client(tmp_path: Path, handler: httpx.MockTransport) -> TestClient:
    app = create_orders(
        OrderSettings(
            "acme", "tok", sqlite_url(tmp_path / "acme.db"), user_account_url="http://ua"
        ),
        http_client=async_client(transport=handler),
    )
    return TestClient(app, headers=HEADERS)


class TestUserAccount:
    def test_user_lifecycle(self, tmp_path: Path) -> None:
        client = users_client(tmp_path)
        user = client.post("/v1/users", json={"email": "Ana@Acme.TEST", "name": "Ana"}).json()
        assert user["email"] == "ana@acme.test"  # normalised
        assert client.get(f"/v1/users/{user['id']}").json() == user
        assert client.delete(f"/v1/users/{user['id']}").status_code == 204
        missing = client.get(f"/v1/users/{user['id']}")
        assert (missing.status_code, missing.json()["error"]) == (404, "user_not_found")
        assert client.delete(f"/v1/users/{user['id']}").status_code == 404

    def test_emails_are_unique_per_tenant(self, tmp_path: Path) -> None:
        client = users_client(tmp_path)
        client.post("/v1/users", json={"email": "a@acme.test", "name": "A"})
        duplicate = client.post("/v1/users", json={"email": "A@acme.test", "name": "B"})
        assert (duplicate.status_code, duplicate.json()["error"]) == (409, "email_exists")

    @pytest.mark.parametrize(
        "body",
        [{"email": "not-an-email", "name": "A"}, {"email": "a@b.co", "name": ""}, {"name": "A"}],
    )
    def test_validation(self, tmp_path: Path, body: dict[str, str]) -> None:
        response = users_client(tmp_path).post("/v1/users", json=body)
        assert (response.status_code, response.json()["error"]) == (422, "validation_error")

    def test_invalid_idempotency_key(self, tmp_path: Path) -> None:
        response = users_client(tmp_path).post(
            "/v1/users",
            json={"email": "a@acme.test", "name": "A"},
            headers={"Idempotency-Key": "x" * 300},
        )
        assert (response.status_code, response.json()["error"]) == (400, "invalid_idempotency_key")

    def test_limit_is_bounded(self, tmp_path: Path) -> None:
        assert users_client(tmp_path).get("/v1/users", params={"limit": 0}).status_code == 422


def user_account_stub(known: set[str], status: int | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers[INTERNAL_TOKEN_HEADER] == "tok"
        assert request.headers[TENANT_HEADER] == "acme"
        if status is not None:
            return httpx.Response(status)
        user_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200 if user_id in known else 404, json={})

    return httpx.MockTransport(handler)


ORDER = {"user_id": "usr_1", "lines": [{"sku": "a", "quantity": 3, "unit_price_cents": 100}]}


class TestOrderProcessing:
    def test_orders_are_filtered_by_user(self, tmp_path: Path) -> None:
        client = orders_client(tmp_path, user_account_stub({"usr_1", "usr_2"}))
        client.post("/v1/orders", json=ORDER)
        client.post("/v1/orders", json={**ORDER, "user_id": "usr_2"})
        listed = client.get("/v1/orders", params={"user_id": "usr_2"}).json()["items"]
        assert [o["user_id"] for o in listed] == ["usr_2"]
        assert listed[0]["total_cents"] == 300

    def test_orders_of_several_users_in_one_call(self, tmp_path: Path) -> None:
        client = orders_client(tmp_path, user_account_stub({"usr_1", "usr_2", "usr_3"}))
        for user in ("usr_1", "usr_2", "usr_3"):
            client.post("/v1/orders", json={**ORDER, "user_id": user})
        listed = client.get("/v1/orders", params=[("user_id", "usr_1"), ("user_id", "usr_3")])
        assert [o["user_id"] for o in listed.json()["items"]] == ["usr_1", "usr_3"]
        too_many = client.get("/v1/orders", params=[("user_id", f"u{i}") for i in range(51)])
        assert (too_many.status_code, too_many.json()["error"]) == (422, "too_many_users")

    def test_unknown_order(self, tmp_path: Path) -> None:
        client = orders_client(tmp_path, user_account_stub(set()))
        for response in (client.get("/v1/orders/nope"), client.post("/v1/orders/nope/cancel")):
            assert (response.status_code, response.json()["error"]) == (404, "order_not_found")

    def test_user_account_errors_are_dependency_failures(self, tmp_path: Path) -> None:
        client = orders_client(tmp_path, user_account_stub(set(), status=500))
        response = client.post("/v1/orders", json=ORDER)
        assert (response.status_code, response.json()["error"]) == (503, "dependency_unavailable")

    @pytest.mark.parametrize(
        "lines",
        [
            [],
            [{"sku": "a", "quantity": 0, "unit_price_cents": 1}],
            [{"sku": "a", "quantity": 1, "unit_price_cents": -1}],
            [{"sku": "bad sku", "quantity": 1, "unit_price_cents": 1}],
        ],
    )
    def test_line_validation(self, tmp_path: Path, lines: list[dict[str, object]]) -> None:
        client = orders_client(tmp_path, user_account_stub({"usr_1"}))
        response = client.post("/v1/orders", json={"user_id": "usr_1", "lines": lines})
        assert response.status_code == 422

    def test_paging_orders(self, tmp_path: Path) -> None:
        client = orders_client(tmp_path, user_account_stub({"usr_1"}))
        for _ in range(3):
            client.post("/v1/orders", json=ORDER)
        first = client.get("/v1/orders", params={"limit": 2}).json()
        second = client.get("/v1/orders", params={"limit": 2, "cursor": first["next_cursor"]})
        assert len(first["items"]) == 2 and len(second.json()["items"]) == 1
        assert second.json()["next_cursor"] is None

    def test_settings_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_ID", "acme")
        monkeypatch.setenv("STACK_TOKEN", "s")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///data/acme.db")
        monkeypatch.setenv("USER_ACCOUNT_URL", "http://ua:8000")
        assert OrderSettings.from_env().user_account_url == "http://ua:8000"

    def test_owned_http_client_is_closed(self, tmp_path: Path) -> None:
        app = create_orders(
            OrderSettings("acme", "tok", sqlite_url(tmp_path / "a.db"), "http://ua")
        )
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
