"""Web UI sessions: tenant hosts and signed OIDC-style tokens (docs/plan.md §2.4).

Every tenant has its own host (``acme.platform.com``; ``acme.localhost`` locally): the
host picks the tenant. A session proves the user may use it: the identity provider
(chosen with the infrastructure; the dev identity provider locally) signs an RS256
token with a tenant claim, and the router verifies it with the provider's public keys
(a JWKS document, fetched once and cached) and checks the claim against the host.
Token checking only, so the router stays the thinnest layer. The browser never sets
the tenant itself.

Static stability: keys are cached, so a provider that is briefly down doesn't stop
verification; an unknown key id triggers at most one refetch per interval.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import jwt
from jwt import PyJWK

from cellarch.common.clock import Clock
from cellarch.common.ids import is_valid_tenant_id

SESSION_COOKIE = "cellarch_session"
TENANT_CLAIM = "tenant_id"
ALGORITHM = "RS256"

JwksFetcher = Callable[[], Awaitable[dict[str, Any]]]


class InvalidSessionError(Exception):
    """The token is missing, malformed, expired, or not signed by the provider."""


class SessionVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        fetch_jwks: JwksFetcher,
        clock: Clock = time.time,
        refetch_interval_seconds: float = 60.0,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._fetch = fetch_jwks
        self._clock = clock
        self._interval = refetch_interval_seconds
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None

    async def _key(self, kid: str) -> PyJWK:
        if kid not in self._keys:
            now = self._clock()
            if self._fetched_at is None or now - self._fetched_at >= self._interval:
                self._fetched_at = now
                try:
                    document = await self._fetch()
                    keys = {k["kid"]: PyJWK(k) for k in document.get("keys", []) if "kid" in k}
                except Exception as exc:  # provider down: keep the keys we have
                    if not self._keys:
                        raise InvalidSessionError(
                            "the identity provider's keys are unavailable"
                        ) from exc
                else:
                    self._keys = keys
        if kid not in self._keys:
            raise InvalidSessionError("the token is signed with an unknown key")
        return self._keys[kid]

    async def tenant(self, token: str | None) -> str:
        """The tenant a valid session token belongs to."""
        if not token:
            raise InvalidSessionError("no session")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidSessionError("the token is malformed") from exc
        if header.get("alg") != ALGORITHM or not isinstance(header.get("kid"), str):
            raise InvalidSessionError("the token is not signed with the expected algorithm")
        key = await self._key(header["kid"])
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            raise InvalidSessionError(f"the token is not valid: {type(exc).__name__}") from exc
        tenant = claims.get(TENANT_CLAIM)
        if not isinstance(tenant, str) or not is_valid_tenant_id(tenant):
            raise InvalidSessionError("the token carries no valid tenant")
        return tenant


def jwks_document(public_jwk: dict[str, Any]) -> dict[str, Any]:
    return {"keys": [public_jwk]}


def tenant_from_host(host: str | None, base_domain: str) -> str | None:
    """``acme`` for ``acme.platform.com`` (or ``acme.localhost:8080``); else ``None``."""
    if not host:
        return None
    name = host.rsplit(":", 1)[0].lower() if host.count(":") == 1 else host.lower()
    suffix = "." + base_domain.lower().strip(".")
    if not name.endswith(suffix):
        return None
    tenant = name[: -len(suffix)]
    return tenant if "." not in tenant and is_valid_tenant_id(tenant) else None
