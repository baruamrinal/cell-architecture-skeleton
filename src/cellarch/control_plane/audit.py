"""Audit log: every admin change, who made it and how it ended (docs/plan.md §5).

Handlers wrap a change in ``audit.record(...)``; the event is written when the block
ends: ``succeeded``, ``refused`` (the API answered 4xx) or ``failed`` (5xx, or an
unexpected error). On AWS the log is also shipped to CloudWatch Logs (stage 5).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from cellarch.common.errors import ApiError
from cellarch.common.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from cellarch.control_plane.store import ControlPlaneStore


@dataclass
class Event:
    """What a handler knows about its change; it may fill in more as it goes."""

    tenant_id: str | None = None
    cell_id: str | None = None
    target_cell_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class AuditEvent(BaseModel):
    event_id: int
    at: str
    actor: str
    action: str
    outcome: str
    error: str | None
    tenant_id: str | None
    cell_id: str | None
    target_cell_id: str | None
    detail: dict[str, Any]


class AuditPage(BaseModel):
    items: list[AuditEvent]
    next_cursor: str | None


class AuditLog:
    def __init__(self, store: ControlPlaneStore) -> None:
        self._store = store

    @contextmanager
    def record(
        self,
        action: str,
        actor: str,
        *,
        tenant_id: str | None = None,
        cell_id: str | None = None,
        target_cell_id: str | None = None,
        **detail: Any,
    ) -> Iterator[Event]:
        event = Event(tenant_id, cell_id, target_cell_id, dict(detail))
        try:
            yield event
        except ApiError as exc:
            outcome = "refused" if exc.status < 500 else "failed"
            self._write(action, actor, event, outcome, exc.code)
            raise
        except Exception:
            self._write(action, actor, event, "failed", "internal_error")
            raise
        self._write(action, actor, event, "succeeded", None)

    def _write(
        self, action: str, actor: str, event: Event, outcome: str, error: str | None
    ) -> None:
        self._store.add_event(
            actor=actor,
            action=action,
            outcome=outcome,
            error=error,
            tenant_id=event.tenant_id,
            cell_id=event.cell_id,
            target_cell_id=event.target_cell_id,
            detail=event.detail,
        )

    def router(self, *, dependencies: list[Any]) -> APIRouter:
        router = APIRouter(prefix="/v1", dependencies=dependencies)

        @router.get("/audit")
        def list_audit(
            tenant_id: str | None = None,
            cell_id: str | None = None,
            limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
            cursor: str | None = None,
        ) -> AuditPage:
            """Newest first. ``next_cursor`` continues with older events."""
            try:
                before = decode_cursor(cursor) if cursor is not None else None
            except ValueError as exc:
                raise ApiError(400, "invalid_cursor", "the cursor is not valid") from exc
            rows = self._store.list_events(
                limit=limit + 1, before=before, tenant_id=tenant_id, cell_id=cell_id
            )
            items = [AuditEvent.model_validate(r) for r in rows[:limit]]
            more = len(rows) > limit
            return AuditPage(
                items=items, next_cursor=encode_cursor(items[-1].event_id) if more else None
            )

        return router
