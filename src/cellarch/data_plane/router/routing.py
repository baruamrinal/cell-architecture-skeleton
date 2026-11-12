"""Tenant -> cell resolution with a TTL cache and stale fallback (static stability).

If the control plane is unreachable or unhealthy, the router keeps routing tenants it
already knows using their last-known entry. Only tenants never seen before fail.
"""

import logging
from dataclasses import dataclass

import httpx

from cellarch.common.clock import Clock, monotonic
from cellarch.common.contracts import RoutingEntry
from cellarch.common.headers import INTERNAL_TOKEN_HEADER

log = logging.getLogger("cellarch.router")


class TenantNotFoundError(Exception):
    """The control plane says the tenant does not exist."""


class RoutingUnavailableError(Exception):
    """The control plane cannot be reached and no cached route exists."""


@dataclass(frozen=True)
class CachedRoute:
    entry: RoutingEntry
    fresh: bool


class RoutingCache:
    def __init__(self, ttl_seconds: float, clock: Clock = monotonic) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[RoutingEntry, float]] = {}

    def get(self, tenant_id: str) -> CachedRoute | None:
        found = self._entries.get(tenant_id)
        if found is None:
            return None
        entry, fetched_at = found
        return CachedRoute(entry, fresh=self._clock() - fetched_at < self._ttl)

    def put(self, entry: RoutingEntry) -> None:
        self._entries[entry.tenant_id] = (entry, self._clock())

    def invalidate(self, tenant_id: str) -> None:
        self._entries.pop(tenant_id, None)


class TenantResolver:
    def __init__(
        self, http: httpx.AsyncClient, control_plane_url: str, token: str, cache: RoutingCache
    ) -> None:
        self._http = http
        self._base = control_plane_url.rstrip("/")
        self._token = token
        self._cache = cache

    async def resolve(self, tenant_id: str, *, refresh: bool = False) -> RoutingEntry:
        """Resolve a tenant. ``tenant_id`` must already be validated by the caller."""
        cached = self._cache.get(tenant_id)
        if cached is not None and cached.fresh and not refresh:
            return cached.entry

        try:
            response = await self._http.get(
                f"{self._base}/v1/routing/tenants/{tenant_id}",
                headers={INTERNAL_TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as exc:
            return self._fallback(tenant_id, cached, f"unreachable: {exc}")

        if response.status_code == 404:
            self._cache.invalidate(tenant_id)
            raise TenantNotFoundError(tenant_id)
        if response.status_code != 200:
            return self._fallback(tenant_id, cached, f"HTTP {response.status_code}")
        try:
            entry = RoutingEntry.model_validate(response.json())
        except ValueError as exc:
            return self._fallback(tenant_id, cached, f"bad response: {exc}")

        self._cache.put(entry)
        return entry

    def _fallback(self, tenant_id: str, cached: CachedRoute | None, reason: str) -> RoutingEntry:
        if cached is None:
            log.error("control plane %s and no cached route for %s", reason, tenant_id)
            raise RoutingUnavailableError(tenant_id)
        log.warning("control plane %s; serving last-known route for %s", reason, tenant_id)
        return cached.entry
