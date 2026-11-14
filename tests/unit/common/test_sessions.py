"""Web UI sessions: tenant hosts and token verification (docs/plan.md §2.4)."""

from __future__ import annotations

import asyncio
from typing import Any

import jwt
import pytest

from cellarch.common.sessions import InvalidSessionError, SessionVerifier, tenant_from_host
from cellarch.dev_idp.app import DevIdpSettings, create_app, issue_token


@pytest.mark.parametrize(
    ("host", "tenant"),
    [
        ("acme.localhost", "acme"),
        ("acme.localhost:8080", "acme"),
        ("ACME.Platform.com", "acme"),
        ("localhost", None),
        ("a.b.localhost", None),
        ("evil.com", None),
        ("x.localhost", None),  # too short to be a tenant id
        (None, None),
        ("[::1]:8080", None),
    ],
)
def test_tenant_from_host(host: str | None, tenant: str | None) -> None:
    domain = "platform.com" if host and "platform" in host.lower() else "localhost"
    assert tenant_from_host(host, domain) == tenant


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def idp() -> Any:
    return create_app(DevIdpSettings(issuer="http://idp", cookie_secure=False))


def verifier(idp: Any, clock: Clock, fetches: list[int], *, fail: bool = False) -> SessionVerifier:
    async def fetch() -> dict[str, Any]:
        fetches.append(1)
        if fail:
            raise ConnectionError("provider down")
        from fastapi.testclient import TestClient

        result: dict[str, Any] = TestClient(idp).get("/auth/.well-known/jwks.json").json()
        return result

    return SessionVerifier(
        issuer="http://idp", audience="cellarch-web", fetch_jwks=fetch, clock=clock
    )


def tenant(v: SessionVerifier, token: str | None) -> str:
    return asyncio.run(v.tenant(token))


def test_keys_are_cached_and_refetched_for_an_unknown_key(idp: Any) -> None:
    clock, fetches = Clock(), list[int]()
    v = verifier(idp, clock, fetches)
    assert tenant(v, issue_token(idp, "acme")) == "acme"
    assert tenant(v, issue_token(idp, "acme")) == "acme"
    assert len(fetches) == 1  # cached

    idp.state.kid = "rotated"  # the provider rotated its key id
    with pytest.raises(InvalidSessionError, match="unknown key"):
        tenant(v, issue_token(idp, "acme"))  # refetch not due yet
    clock.now += 61
    with pytest.raises(InvalidSessionError, match="unknown key"):
        tenant(v, issue_token(idp, "acme"))  # refetched; still not published
    assert len(fetches) == 2


def test_a_provider_that_is_down_keeps_the_cached_keys(idp: Any) -> None:
    clock, fetches = Clock(), list[int]()
    v = verifier(idp, clock, fetches)
    assert tenant(v, issue_token(idp, "acme")) == "acme"
    v._fetch = verifier(idp, clock, fetches, fail=True)._fetch
    clock.now += 61
    idp.state.kid = "other"
    with pytest.raises(InvalidSessionError, match="unknown key"):
        tenant(v, issue_token(idp, "acme"))
    assert v._keys  # the old keys are still there


def test_without_any_keys_a_provider_outage_refuses_sessions(idp: Any) -> None:
    v = verifier(idp, Clock(), [], fail=True)
    with pytest.raises(InvalidSessionError, match="unavailable"):
        tenant(v, issue_token(idp, "acme"))


def test_tokens_with_another_algorithm_or_bad_claims_are_refused(idp: Any) -> None:
    v = verifier(idp, Clock(), [])
    hs256 = jwt.encode({"sub": "x"}, "secret-secret-secret-secret-secret", algorithm="HS256")
    with pytest.raises(InvalidSessionError, match="algorithm"):
        tenant(v, hs256)
    no_tenant = jwt.encode(
        {"iss": "http://idp", "aud": "cellarch-web", "sub": "x", "iat": 1, "exp": 4_000_000_000},
        idp.state.key,
        algorithm="RS256",
        headers={"kid": idp.state.kid},
    )
    with pytest.raises(InvalidSessionError, match="no valid tenant"):
        tenant(v, no_tenant)
    with pytest.raises(InvalidSessionError, match="no session"):
        tenant(v, None)


def test_dev_idp_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_BASE_DOMAIN", "platform.test")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    settings = DevIdpSettings.from_env()
    assert (settings.base_domain, settings.cookie_secure) == ("platform.test", False)
    from fastapi.testclient import TestClient

    assert TestClient(create_app(settings)).get("/auth/healthz").json() == {"status": "ok"}
