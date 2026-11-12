"""Service-to-service authentication with role-scoped tokens.

Each caller role (admin, router, control-plane, ...) has its own token, so a leaked
router token cannot call admin APIs and one cell's token cannot call another cell.
On AWS this is replaced by IAM / workload identity (docs/plan.md §2.3).
"""

import hmac
from collections.abc import Mapping
from typing import Any

from fastapi import Depends, Header

from cellarch.common.errors import ApiError
from cellarch.common.headers import INTERNAL_TOKEN_HEADER


class TokenAuth:
    def __init__(self, tokens_by_role: Mapping[str, str]) -> None:
        for role, token in tokens_by_role.items():
            if not token:
                raise ValueError(f"empty token for role {role!r}")
        if len(set(tokens_by_role.values())) != len(tokens_by_role):
            raise ValueError("tokens must be distinct per role")
        self._tokens = dict(tokens_by_role)

    def role_for(self, token: str | None) -> str | None:
        if not token:
            return None
        matched = None
        for role, expected in self._tokens.items():
            if hmac.compare_digest(token.encode(), expected.encode()):
                matched = role
        return matched

    def require(self, *roles: str) -> Any:
        unknown = set(roles) - set(self._tokens)
        if unknown:
            raise ValueError(f"unknown role(s): {sorted(unknown)}")

        async def verify(
            token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
        ) -> str:
            role = self.role_for(token)
            if role is None:
                raise ApiError(401, "unauthenticated", "missing or invalid credential")
            if role not in roles:
                raise ApiError(403, "forbidden", "credential is not allowed to use this API")
            return role

        return Depends(verify)
