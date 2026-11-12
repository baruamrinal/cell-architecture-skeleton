from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cellarch.common.config import ConfigError, parse_pairs, require_env
from cellarch.common.contracts import is_valid_tenant_id
from cellarch.common.errors import ApiError, install_error_handlers
from cellarch.common.headers import INTERNAL_TOKEN_HEADER
from cellarch.common.security import TokenAuth


class TestParsePairs:
    def test_parses_comma_separated_pairs(self) -> None:
        assert parse_pairs("cell-1=http://a:8000, cell-2=http://b") == {
            "cell-1": "http://a:8000",
            "cell-2": "http://b",
        }

    def test_empty_string_is_empty(self) -> None:
        assert parse_pairs("") == {}
        assert parse_pairs("  ") == {}

    def test_value_may_contain_equals(self) -> None:
        assert parse_pairs("k=a=b") == {"k": "a=b"}

    @pytest.mark.parametrize("raw", ["novalue", "=x", "k="])
    def test_malformed_pairs_raise(self, raw: str) -> None:
        with pytest.raises(ConfigError):
            parse_pairs(raw)


class TestRequireEnv:
    def test_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_VAR", "x")
        assert require_env("SOME_VAR") == "x"

    @pytest.mark.parametrize("value", [None, ""])
    def test_missing_or_empty_raises(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        if value is None:
            monkeypatch.delenv("SOME_VAR", raising=False)
        else:
            monkeypatch.setenv("SOME_VAR", value)
        with pytest.raises(ConfigError, match="SOME_VAR"):
            require_env("SOME_VAR")


@pytest.mark.parametrize("tenant_id", ["acme", "a1", "acme-corp", "t-0123456789abcdef"])
def test_valid_tenant_ids(tenant_id: str) -> None:
    assert is_valid_tenant_id(tenant_id)


@pytest.mark.parametrize("tenant_id", ["", "a", "Acme", "-acme", "acme_corp", "../x", "a" * 64])
def test_invalid_tenant_ids(tenant_id: str) -> None:
    assert not is_valid_tenant_id(tenant_id)


def make_app() -> FastAPI:
    auth = TokenAuth({"admin": "admin-secret", "router": "router-secret"})
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/admin", dependencies=[auth.require("admin")])
    def admin_only() -> dict[str, str]:
        return {"ok": "admin"}

    @app.get("/either", dependencies=[auth.require("admin", "router")])
    def either() -> dict[str, str]:
        return {"ok": "either"}

    @app.get("/boom")
    def boom() -> None:
        raise ApiError(409, "conflict_here", "already exists", headers={"Retry-After": "3"})

    @app.get("/items/{n}")
    def item(n: int) -> dict[str, int]:
        return {"n": n}

    return app


class TestTokenAuth:
    client = TestClient(make_app())

    @pytest.mark.parametrize(
        ("path", "token", "status"),
        [
            ("/admin", None, 401),
            ("/admin", "nope", 401),
            ("/admin", "router-secret", 403),
            ("/admin", "admin-secret", 200),
            ("/either", "router-secret", 200),
            ("/either", "admin-secret", 200),
        ],
    )
    def test_roles(self, path: str, token: str | None, status: int) -> None:
        headers = {} if token is None else {INTERNAL_TOKEN_HEADER: token}
        response = self.client.get(path, headers=headers)
        assert response.status_code == status
        if status in (401, 403):
            assert response.json()["error"] in ("unauthenticated", "forbidden")

    def test_rejects_empty_token_configuration(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            TokenAuth({"admin": ""})

    def test_rejects_duplicate_tokens_across_roles(self) -> None:
        with pytest.raises(ValueError, match="distinct"):
            TokenAuth({"admin": "same", "router": "same"})

    def test_rejects_unknown_role(self) -> None:
        with pytest.raises(ValueError, match="unknown role"):
            TokenAuth({"admin": "x"}).require("root")


class TestErrorHandlers:
    client = TestClient(make_app())

    def test_api_error_body_and_headers(self) -> None:
        response = self.client.get("/boom")
        assert response.status_code == 409
        assert response.json() == {"error": "conflict_here", "message": "already exists"}
        assert response.headers["Retry-After"] == "3"

    def test_validation_errors_use_the_same_shape(self) -> None:
        response = self.client.get("/items/not-a-number")
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "validation_error"
        assert body["details"]

    def test_unknown_route_uses_the_same_shape(self) -> None:
        response = self.client.get("/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
