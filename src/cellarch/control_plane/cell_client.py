"""Calls from the control plane to a cell, using that cell's own credential."""

from typing import Protocol

import httpx

from cellarch.common.headers import INTERNAL_TOKEN_HEADER


class MissingCellCredentialError(Exception):
    """No credential is configured for the cell."""


class CellVerificationError(Exception):
    """A cell failed the checks done before it is registered."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CredentialLookup(Protocol):
    def get(self, cell_id: str, /) -> str | None: ...


class CellClient:
    def __init__(self, http: httpx.AsyncClient, credentials: CredentialLookup) -> None:
        self._http = http
        self._credentials = credentials

    def has_credential(self, cell_id: str) -> bool:
        return bool(self._credentials.get(cell_id))

    async def verify(self, cell_id: str, cell_url: str, token: str) -> None:
        """Check the cell is up, is the cell it claims to be, and accepts the credential."""
        base = cell_url.rstrip("/")
        try:
            health = await self._http.get(f"{base}/healthz")
        except httpx.HTTPError as exc:
            raise CellVerificationError(
                "cell_unreachable", f"cannot reach {cell_url}: {exc}"
            ) from exc
        if health.status_code != 200:
            raise CellVerificationError(
                "cell_unreachable", f"{cell_url} health check returned HTTP {health.status_code}"
            )
        try:
            reported = health.json().get("cell_id")
        except ValueError:
            reported = None
        if reported != cell_id:
            raise CellVerificationError(
                "cell_id_mismatch", f"{cell_url} reports cell id {reported!r}, not {cell_id!r}"
            )

        try:
            probe = await self._http.get(
                f"{base}/internal/tenants", headers={INTERNAL_TOKEN_HEADER: token}
            )
        except httpx.HTTPError as exc:
            raise CellVerificationError(
                "cell_unreachable", f"cannot reach {cell_url}: {exc}"
            ) from exc
        if probe.status_code in (401, 403):
            raise CellVerificationError(
                "cell_credential_rejected", f"{cell_id} rejected the credential"
            )
        if probe.status_code != 200:
            raise CellVerificationError(
                "cell_unreachable", f"{cell_url} internal API returned HTTP {probe.status_code}"
            )

    async def provision_tenant(
        self, cell_id: str, cell_url: str, tenant_id: str, isolation: str = "pooled"
    ) -> None:
        body = {"tenant_id": tenant_id, "isolation": isolation}
        await self._call("POST", cell_id, cell_url, "/internal/tenants", body)

    async def _call(
        self, method: str, cell_id: str, cell_url: str, path: str, body: object = None
    ) -> httpx.Response:
        token = self._credentials.get(cell_id)
        if not token:
            raise MissingCellCredentialError(cell_id)
        response = await self._http.request(
            method,
            f"{cell_url.rstrip('/')}{path}",
            json=body,
            headers={INTERNAL_TOKEN_HEADER: token},
        )
        response.raise_for_status()
        return response
