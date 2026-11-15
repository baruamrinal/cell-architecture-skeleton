"""In-process stand-ins for what runs inside a cell's Kubernetes cluster.

* ``MemorySecrets``: the cell's secret store (Kubernetes Secrets; Secrets Manager on AWS).
* ``StackHost``: the tenants' stacks the ``tenant-stack`` Helm chart starts. Each stack is
  the real user-account and order-processing apps, configured from the tenant's secret
  exactly as the chart configures the containers, and reachable at the address the
  gateway's ``TENANT_SERVICE_URL`` template gives.
"""

from __future__ import annotations

import ssl
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.types import ASGIApp, Receive, Scope, Send

from cellarch.data_plane.cell.tenant_resources import (
    SERVICES,
    STACK_TOKEN_KEY,
    TenantResourceError,
    database_url_key,
)
from cellarch.services.common.settings import ServiceSettings
from cellarch.services.order_processing.app import OrderSettings
from cellarch.services.order_processing.app import create_app as create_order_processing
from cellarch.services.user_account.app import create_app as create_user_account

# Building a TLS context loads the system's CA bundle: about half a second per client
# on Windows. Tests never open real TLS connections, so every test client shares one.
_TLS = ssl.create_default_context()


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """An httpx.AsyncClient for tests, without building its own TLS context."""
    return httpx.AsyncClient(verify=_TLS, **kwargs)


class MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}
        self.fail = False  # writes fail
        self.fail_delete = False

    def put(self, tenant_id: str, values: dict[str, str]) -> None:
        if self.fail:
            raise TenantResourceError("secret store unavailable")
        self.values[tenant_id] = dict(values)

    def delete(self, tenant_id: str) -> None:
        if self.fail_delete:
            raise TenantResourceError("secret store unavailable")
        self.values.pop(tenant_id, None)


def stack_url_template(cell_id: str) -> str:
    """Per-cell service addresses, so one host can serve every cell's stacks."""
    return f"http://{{service}}.{{tenant}}.{cell_id}.stacks"


class StackHost:
    """ASGI app routing by host name to the running tenant services."""

    def __init__(self) -> None:
        self.apps: dict[str, ASGIApp] = {}
        self.down: set[str] = set()  # tenants whose stack is unreachable

    def client(self) -> httpx.AsyncClient:
        return async_client(transport=httpx.ASGITransport(app=self))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"").decode().split(":")[0]
        app = self.apps.get(host)
        tenant = host.split(".")[1] if host.count(".") >= 1 else ""
        if app is None or tenant in self.down:
            raise httpx.ConnectError(f"nothing is running at {host}")
        await app(scope, receive, send)

    def start(self, cell_id: str, tenant_id: str, secret: dict[str, str]) -> None:
        """What `helm install tenant-<id> charts/tenant-stack` does in the cell's cluster."""
        template = stack_url_template(cell_id)
        urls = {s: template.format(service=s, tenant=tenant_id) for s in SERVICES}
        token = secret[STACK_TOKEN_KEY]
        user_account = create_user_account(
            ServiceSettings(tenant_id, token, secret[database_url_key("user-account")])
        )
        order_processing = create_order_processing(
            OrderSettings(
                tenant_id,
                token,
                secret[database_url_key("order-processing")],
                user_account_url=urls["user-account"],
            ),
            http_client=self.client(),
        )
        self.apps[_host(urls["user-account"])] = user_account
        self.apps[_host(urls["order-processing"])] = order_processing

    def stop(self, cell_id: str, tenant_id: str) -> None:
        template = stack_url_template(cell_id)
        for service in SERVICES:
            self.apps.pop(_host(template.format(service=service, tenant=tenant_id)), None)

    def running(self, cell_id: str, tenant_id: str) -> bool:
        host = _host(stack_url_template(cell_id).format(service=SERVICES[0], tenant=tenant_id))
        return host in self.apps


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""
