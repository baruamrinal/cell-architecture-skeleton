"""Calls from the control plane to a cell's /internal API, using that cell's credential."""

from collections.abc import Mapping

import httpx

from cellarch.common.headers import INTERNAL_TOKEN_HEADER


class MissingCellCredentialError(Exception):
    """No credential is configured for the cell."""


class CellClient:
    def __init__(self, http: httpx.AsyncClient, credentials: Mapping[str, str]) -> None:
        self._http = http
        self._credentials = dict(credentials)

    def has_credential(self, cell_id: str) -> bool:
        return bool(self._credentials.get(cell_id))

    async def provision_tenant(self, cell_id: str, cell_url: str, tenant_id: str) -> None:
        await self._call("POST", cell_id, cell_url, "/internal/tenants", {"tenant_id": tenant_id})

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
