"""Per-tenant usage counters in the router (docs/plan.md §4).

Kept in memory and reset when the router restarts; each router replica counts its own
traffic. Stage 6 exports the same numbers as metrics (OpenTelemetry -> CloudWatch).
Sustained throttling is the signal to move a tenant to a dedicated cell (stage 3).
"""

from dataclasses import dataclass, field
from typing import Any

from cellarch.common.db import utcnow

_CLASSES = ("2xx", "3xx", "4xx", "5xx")


@dataclass
class _Usage:
    requests: int = 0
    throttled: int = 0
    responses: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_CLASSES, 0))


class Metering:
    def __init__(self) -> None:
        self.since = utcnow()
        self._usage: dict[str, _Usage] = {}

    def record(self, tenant_id: str, status: int, *, throttled: bool = False) -> None:
        """Count one request from a known tenant (callers never pass unknown ids)."""
        usage = self._usage.setdefault(tenant_id, _Usage())
        usage.requests += 1
        if throttled:
            usage.throttled += 1
        status_class = f"{status // 100}xx"
        if status_class in usage.responses:
            usage.responses[status_class] += 1

    def snapshot(self) -> list[dict[str, Any]]:
        """Busiest tenants first."""
        ordered = sorted(self._usage.items(), key=lambda item: (-item[1].requests, item[0]))
        return [
            {
                "tenant_id": tenant_id,
                "requests": usage.requests,
                "throttled": usage.throttled,
                "responses": dict(usage.responses),
            }
            for tenant_id, usage in ordered
        ]
