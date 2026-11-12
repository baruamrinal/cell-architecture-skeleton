"""The control plane's admin API as the operator tools use it (cellctl, the demo)."""

from typing import Any, Protocol

import httpx

from cellarch.common.headers import ACTOR_HEADER, INTERNAL_TOKEN_HEADER


class CommandError(Exception):
    """An API call or step failed; reported as ``error: <code>: <message>``."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class AdminApi(Protocol):
    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any: ...


def make_client(url: str) -> httpx.Client:
    return httpx.Client(base_url=url, timeout=30.0)


class Api:
    def __init__(self, http: httpx.Client, token: str, actor: str | None = None) -> None:
        self._http = http
        self._headers = {INTERNAL_TOKEN_HEADER: token}
        if actor:
            self._headers[ACTOR_HEADER] = actor

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = self._http.request(method, path, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise CommandError("unreachable", f"cannot reach control plane: {exc}") from exc
        data = response.json() if response.content else None
        if response.status_code >= 400:
            info = data if isinstance(data, dict) else {}
            raise CommandError(
                str(info.get("error", "error")),
                str(info.get("message", response.text)),
                response.status_code,
            )
        return data
