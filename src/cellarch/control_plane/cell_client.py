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

    async def provision_tenant(self, cell_id: str, cell_url: str, tenant_id: str) -> None:
        """The cell creates the tenant's database, logins and stack secret."""
        await self._call("POST", cell_id, cell_url, "/internal/tenants", {"tenant_id": tenant_id})

    async def activate_tenant(self, cell_id: str, cell_url: str, tenant_id: str) -> None:
        """The cell checks the tenant's stack is ready (HTTP 409 if not)."""
        await self._call("POST", cell_id, cell_url, f"/internal/tenants/{tenant_id}/activate")

    async def delete_tenant(self, cell_id: str, cell_url: str, tenant_id: str) -> None:
        """The cell drops the tenant's database, logins and stack secret."""
        await self._call("DELETE", cell_id, cell_url, f"/internal/tenants/{tenant_id}")

    async def freeze_tenant(self, cell_id: str, cell_url: str, tenant_id: str) -> None:
        """The cell stops taking the tenant's writes (a move is starting)."""
        await self._call("POST", cell_id, cell_url, f"/internal/tenants/{tenant_id}/freeze")

    async def unfreeze_tenant(self, cell_id: str, cell_url: str, tenant_id: str) -> None:
        """The cell takes the tenant's writes again (the move was rolled back)."""
        await self._call("POST", cell_id, cell_url, f"/internal/tenants/{tenant_id}/unfreeze")

    async def copy_tenant(
        self,
        tenant_id: str,
        source: tuple[str, str],
        target: tuple[str, str],
    ) -> dict[str, int]:
        """Stream the tenant's export from the source cell into the target cell.

        Cells never call each other, so the control plane relays the stream, with each
        cell's own credential. Returns the rows imported per table. On AWS each cell
        gets a pre-signed S3 URL instead, so the data bypasses the control plane.
        """
        (source_id, source_url), (target_id, target_url) = source, target
        export_url = f"{source_url.rstrip('/')}/internal/tenants/{tenant_id}/export"
        import_url = f"{target_url.rstrip('/')}/internal/tenants/{tenant_id}/import"
        async with self._http.stream(
            "GET", export_url, headers={INTERNAL_TOKEN_HEADER: self._token(source_id)}
        ) as export:
            if export.status_code >= 400:
                await export.aread()
                export.raise_for_status()
            response = await self._http.post(
                import_url,
                content=export.aiter_bytes(),
                headers={
                    INTERNAL_TOKEN_HEADER: self._token(target_id),
                    "content-type": "application/x-ndjson",
                },
            )
        response.raise_for_status()
        tables: dict[str, int] = response.json()["tables"]
        return tables

    def _token(self, cell_id: str) -> str:
        token = self._credentials.get(cell_id)
        if not token:
            raise MissingCellCredentialError(cell_id)
        return token

    async def _call(
        self, method: str, cell_id: str, cell_url: str, path: str, body: object = None
    ) -> httpx.Response:
        token = self._token(cell_id)
        response = await self._http.request(
            method,
            f"{cell_url.rstrip('/')}{path}",
            json=body,
            headers={INTERNAL_TOKEN_HEADER: token},
        )
        response.raise_for_status()
        return response
