"""mgr's only way to tenant data: its cell gateway's ``/v1`` API, for one tenant.

The gateway checks that the cell hosts the tenant, pauses writes while the tenant is
being moved and adds the tenant's stack credential, so mgr holds no credentials of
its own for the tenant services. Every call mgr makes is listed in contract.py.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from cellarch.common.headers import REQUEST_ID_HEADER, TENANT_HEADER

IDEMPOTENCY_HEADER = "Idempotency-Key"


class GatewayError(Exception):
    """The gateway or a tenant service refused or failed; carried into GraphQL errors."""

    def __init__(
        self, status: int, code: str, message: str, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class Gateway:
    def __init__(
        self, http: httpx.AsyncClient, base_url: str, tenant_id: str, request_id: str | None
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._headers = {TENANT_HEADER: tenant_id}
        if request_id:
            self._headers[REQUEST_ID_HEADER] = request_id

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
        idempotency_key: str | None = None,
        not_found_is_none: bool = False,
    ) -> Any:
        headers = dict(self._headers)
        if idempotency_key:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        try:
            response = await self._http.request(
                method, f"{self._base}{path}", params=params, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise GatewayError(503, "cell_unavailable", "the cell is unavailable", 1) from exc
        if response.status_code == 404 and not_found_is_none:
            return None
        if response.status_code >= 400:
            try:
                body = response.json()
                code, message = str(body["error"]), str(body["message"])
            except (ValueError, KeyError, TypeError):
                code, message = "upstream_error", f"the cell answered HTTP {response.status_code}"
            retry = response.headers.get("retry-after")
            raise GatewayError(
                response.status_code,
                code,
                message,
                int(retry) if retry and retry.isdigit() else None,
            )
        return response.json() if response.content else None


def segment(value: str) -> str:
    """A path segment from a client-supplied id, so it can't change the path."""
    return quote(value, safe="")
