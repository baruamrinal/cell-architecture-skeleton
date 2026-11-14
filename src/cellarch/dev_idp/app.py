"""Dev identity provider: the local stand-in for the production one (docs/plan.md §2.4).

DEVELOPMENT ONLY. Anyone who reaches a tenant's host can sign in there: it exists so
the web UI runs locally behind the same token interface as production. The production
provider (chosen with the infrastructure, stage 6) issues the same kind of token
(RS256, a ``tenant_id`` claim) after a real sign-in; the router verifies both the same
way.

* ``POST /auth/dev/sign-in`` on ``<tenant>.localhost`` signs in for that host's tenant
  and sets the session cookie (no ``Domain``: it stays on that tenant's host);
* ``POST /auth/sign-out`` clears it;
* ``GET /auth/.well-known/jwks.json`` publishes the public key the router verifies with.

The signing key is generated at start-up (the router refetches keys it doesn't know).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request, Response
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel

from cellarch.common.errors import ApiError, install_error_handlers
from cellarch.common.logging import configure_logging
from cellarch.common.sessions import (
    ALGORITHM,
    SESSION_COOKIE,
    TENANT_CLAIM,
    jwks_document,
    tenant_from_host,
)

log = logging.getLogger("cellarch.dev_idp")


@dataclass(frozen=True)
class DevIdpSettings:
    issuer: str
    audience: str = "cellarch-web"
    base_domain: str = "localhost"  # tenants sign in on <tenant>.<base_domain>
    token_ttl_seconds: int = 8 * 3600
    # Browsers treat http://localhost as secure, so Secure cookies work locally too.
    cookie_secure: bool = True

    @classmethod
    def from_env(cls) -> DevIdpSettings:
        return cls(
            issuer=os.environ.get("DEV_IDP_ISSUER", "http://localhost:8080/auth"),
            audience=os.environ.get("SESSION_AUDIENCE", "cellarch-web"),
            base_domain=os.environ.get("TENANT_BASE_DOMAIN", "localhost"),
            token_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", str(8 * 3600))),
            cookie_secure=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() != "false",
        )


class Session(BaseModel):
    tenant_id: str
    expires_in: int


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def issue_token(
    app: FastAPI, tenant_id: str, *, issued_at: float | None = None, forged: bool = False
) -> str:
    """A session token as the provider issues it (``forged``: signed with another key)."""
    state = app.state
    now = time.time() if issued_at is None else issued_at
    claims = {
        "iss": state.settings.issuer,
        "aud": state.settings.audience,
        "sub": f"dev-{tenant_id}",
        "iat": int(now),
        "exp": int(now) + state.settings.token_ttl_seconds,
        "jti": uuid.uuid4().hex,
        TENANT_CLAIM: tenant_id,
    }
    key = _new_key() if forged else state.key
    return jwt.encode(claims, key, algorithm=ALGORITHM, headers={"kid": state.kid})


def create_app(settings: DevIdpSettings | None = None) -> FastAPI:
    settings = settings or DevIdpSettings.from_env()
    configure_logging("dev-idp")
    app = FastAPI(title="Dev identity provider (development only)")
    install_error_handlers(app)
    app.state.settings = settings
    app.state.key = _new_key()
    app.state.kid = uuid.uuid4().hex[:16]
    public: dict[str, Any] = RSAAlgorithm.to_jwk(app.state.key.public_key(), as_dict=True)
    public.update({"kid": app.state.kid, "use": "sig", "alg": ALGORITHM})
    log.warning("dev identity provider running: anyone can sign in as any tenant (dev only)")

    @app.get("/auth/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/auth/.well-known/jwks.json")
    def jwks() -> dict[str, Any]:
        return jwks_document(public)

    @app.post("/auth/dev/sign-in")
    def sign_in(request: Request, response: Response) -> Session:
        tenant = tenant_from_host(request.headers.get("host"), settings.base_domain)
        if tenant is None:
            raise ApiError(
                400,
                "tenant_host_required",
                f"sign in on your tenant's own host, e.g. acme.{settings.base_domain}",
            )
        token = issue_token(app, tenant)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.token_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        log.info("dev sign-in for tenant %s", tenant)
        return Session(tenant_id=tenant, expires_in=settings.token_ttl_seconds)

    @app.post("/auth/sign-out", status_code=204)
    def sign_out() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    return app
