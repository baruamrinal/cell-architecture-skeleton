"""Tenant moves: one procedure for every move between cells (docs/plan.md §5).

A move is a record that goes through fixed states; the operator's tool (``cellctl``
locally, the pipeline on AWS) calls each step and installs Helm releases in between:

    start     tenant ``migrating``, frozen on the source, database + secret on the target
    copy      the source's export is imported into the target                -> copied
    switch    the target's stack is ready; the route points to the target   -> switched
    complete  the source drops its copy (or the move is flagged)            -> completed
    rollback  before the switch: target cleared, source unfrozen            -> rolled_back

Each step is idempotent once the move is past it, and refused before the move gets
there. After the switch the source is never unfrozen, so nothing can write to the old
copy; a failed clean-up is left as ``cleanup_pending`` and retried with ``complete``.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field

from cellarch.common.contracts import CELL_ID_PATTERN, TENANT_ID_PATTERN
from cellarch.common.errors import ApiError
from cellarch.control_plane.audit import AuditLog
from cellarch.control_plane.cell_client import CellClient
from cellarch.control_plane.placement import NoCapacityError
from cellarch.control_plane.store import ControlPlaneStore

log = logging.getLogger("cellarch.control_plane.moves")

ORDER = ("started", "copied", "switched", "completed")
NOT_READY = "tenant_stack_not_ready"

TenantId = Annotated[str, Path(pattern=TENANT_ID_PATTERN)]


class MoveRequest(BaseModel):
    # Omitted: placement chooses a cell other than the tenant's current one.
    target_cell_id: str | None = Field(default=None, pattern=CELL_ID_PATTERN)


class RollbackRequest(BaseModel):
    reason: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")


class Move(BaseModel):
    move_id: str
    tenant_id: str
    source_cell_id: str
    target_cell_id: str
    state: str
    cleanup_pending: str | None
    error: str | None
    copied_rows: int | None
    created_at: str
    created_by: str
    updated_at: str
    updated_by: str


class MoveList(BaseModel):
    items: list[Move]


def _cell_unavailable(cell_id: str, exc: Exception) -> ApiError:
    log.warning("cell %s call failed during a move: %s", cell_id, exc)
    return ApiError(502, "cell_unavailable", f"cell {cell_id} did not complete the request")


def _cell_refusal(cell_id: str, exc: Exception) -> ApiError:
    """Pass a cell's 4xx answer on (its code says why); anything else is unavailable."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
        try:
            body = exc.response.json()
            return ApiError(exc.response.status_code, str(body["error"]), str(body["message"]))
        except (ValueError, KeyError, TypeError):
            pass
    return _cell_unavailable(cell_id, exc)


class MoveProcedure:
    def __init__(
        self,
        store: ControlPlaneStore,
        cells: CellClient,
        choose_target: Callable[[str], str],
    ) -> None:
        self._store = store
        self._cells = cells
        self._choose_target = choose_target  # placement, excluding the source cell

    def _url(self, cell_id: str) -> str:
        cell = self._store.get_cell(cell_id)
        if cell is None:  # pragma: no cover - cells with tenants or moves can't be removed
            raise ApiError(404, "cell_not_found", f"cell {cell_id} not found")
        return str(cell["url"])

    def require(self, move_id: str) -> dict[str, Any]:
        move = self._store.get_move(move_id)
        if move is None:
            raise ApiError(404, "move_not_found", f"move {move_id} not found")
        return move

    @staticmethod
    def _reached(move: dict[str, Any], state: str) -> bool:
        """True if the move is at or past ``state``; refuses moves that can't get there."""
        if move["state"] in ORDER and ORDER.index(move["state"]) >= ORDER.index(state):
            return True
        if move["state"] == "rolled_back" or ORDER.index(move["state"]) < ORDER.index(state) - 1:
            raise ApiError(
                409,
                "move_state_conflict",
                f"move {move['move_id']} is {move['state']}; it can't be {state} now",
            )
        return False

    # --- start ---------------------------------------------------------------------------

    def _check(self, tenant_id: str, target: str | None) -> tuple[str, str]:
        tenant = self._store.get_tenant(tenant_id)
        if tenant is None:
            raise ApiError(404, "tenant_not_found", f"tenant {tenant_id} not found")
        if self._store.unfinished_move(tenant_id) is not None:
            raise ApiError(409, "move_in_progress", f"tenant {tenant_id} is already being moved")
        if tenant["status"] != "active":
            raise ApiError(409, "tenant_not_active", f"tenant {tenant_id} is {tenant['status']}")
        source = str(tenant["cell_id"])
        if target is None:
            try:
                target = self._choose_target(source)
            except NoCapacityError as exc:
                raise ApiError(503, "no_capacity", "no other cell has room") from exc
        if target == source:
            raise ApiError(422, "same_cell", f"tenant {tenant_id} is already on {target}")
        cell = self._store.get_cell(target)
        if cell is None:
            raise ApiError(404, "cell_not_found", f"cell {target} not found")
        if cell["status"] != "active":
            raise ApiError(409, "cell_not_active", f"cell {target} is {cell['status']}")
        if int(cell["tenant_count"]) + int(cell["incoming_tenants"]) >= int(cell["capacity"]):
            raise ApiError(409, "cell_full", f"cell {target} has no room")
        return source, target

    async def start(self, tenant_id: str, target: str | None, actor: str) -> dict[str, Any]:
        source, target = self._check(tenant_id, target)
        move_id = f"mv_{uuid.uuid4().hex[:12]}"
        move = self._store.add_move(move_id, tenant_id, source, target, actor)
        log.info("moving %s from %s to %s (%s)", tenant_id, source, target, move_id)
        frozen = False
        try:
            await self._cells.freeze_tenant(source, self._url(source), tenant_id)
            frozen = True
            await self._cells.provision_tenant(target, self._url(target), tenant_id)
        except Exception as exc:
            failed_cell = target if frozen else source
            if frozen:  # best effort: the start never got far
                await self._undo(self._cells.unfreeze_tenant, source, tenant_id)
                await self._undo(self._cells.delete_tenant, target, tenant_id)
            self._store.end_move(
                move, actor, state="rolled_back", cell_id=source, error="cell_unavailable"
            )
            raise _cell_unavailable(failed_cell, exc) from exc
        return move

    async def _undo(self, call: Callable[..., Any], cell_id: str, tenant_id: str) -> bool:
        try:
            await call(cell_id, self._url(cell_id), tenant_id)
        except Exception as exc:  # the caller records what is left over
            log.warning("undo on %s for %s failed: %s", cell_id, tenant_id, exc)
            return False
        return True

    # --- steps ---------------------------------------------------------------------------

    async def copy(self, move_id: str, actor: str) -> dict[str, Any]:
        move = self.require(move_id)
        if self._reached(move, "copied"):
            return move
        tenant_id, source, target = (
            move["tenant_id"],
            move["source_cell_id"],
            move["target_cell_id"],
        )
        try:
            tables = await self._cells.copy_tenant(
                tenant_id, (source, self._url(source)), (target, self._url(target))
            )
        except Exception as exc:
            raise _cell_refusal(target, exc) from exc
        return self._store.update_move(
            move_id, actor, state="copied", copied_rows=sum(tables.values())
        )

    async def switch(self, move_id: str, actor: str) -> dict[str, Any]:
        move = self.require(move_id)
        if self._reached(move, "switched"):
            return move
        target = move["target_cell_id"]
        try:
            await self._cells.activate_tenant(target, self._url(target), move["tenant_id"])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                raise ApiError(409, NOT_READY, "the tenant's services are not ready yet") from exc
            raise _cell_unavailable(target, exc) from exc
        except Exception as exc:
            raise _cell_unavailable(target, exc) from exc
        log.info("tenant %s now served by %s", move["tenant_id"], target)
        # A tenant alone on a dedicated cell is dedicated; on a shared cell, pooled.
        cell = self._store.get_cell(target) or {}
        mode = "dedicated" if cell.get("type") == "dedicated" else "pooled"
        return self._store.end_move(
            move, actor, state="switched", cell_id=target, placement_mode=mode
        )

    async def complete(self, move_id: str, actor: str) -> dict[str, Any]:
        move = self.require(move_id)
        if move["state"] == "completed" and not move["cleanup_pending"]:
            return move
        if not self._reached(move, "switched"):
            raise ApiError(
                409, "move_state_conflict", f"move {move_id} has not switched to its target yet"
            )
        source = move["source_cell_id"]
        deleted = await self._undo(self._cells.delete_tenant, source, move["tenant_id"])
        if not deleted:
            log.warning(
                "move %s: %s keeps a copy of %s to clean up", move_id, source, move["tenant_id"]
            )
        return self._store.update_move(
            move_id, actor, state="completed", cleanup_pending=None if deleted else source
        )

    async def rollback(self, move_id: str, reason: str | None, actor: str) -> dict[str, Any]:
        move = self.require(move_id)
        if move["state"] == "rolled_back":
            return move
        if move["state"] not in ("started", "copied"):
            raise ApiError(
                409,
                "move_state_conflict",
                f"move {move_id} already switched to its target; it can't be rolled back",
            )
        tenant_id, source, target = (
            move["tenant_id"],
            move["source_cell_id"],
            move["target_cell_id"],
        )
        try:
            await self._cells.unfreeze_tenant(source, self._url(source), tenant_id)
        except Exception as exc:
            raise _cell_unavailable(source, exc) from exc
        cleared = await self._undo(self._cells.delete_tenant, target, tenant_id)
        log.info("move %s of %s rolled back", move_id, tenant_id)
        return self._store.end_move(
            move,
            actor,
            state="rolled_back",
            cell_id=source,
            error=reason or "rolled_back",
            cleanup_pending=None if cleared else target,
        )


def moves_router(
    procedure: MoveProcedure,
    store: ControlPlaneStore,
    audit: AuditLog,
    current_actor: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(current_actor)])
    Actor = Annotated[str, Depends(current_actor)]

    def recorded(move: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "move_id": move["move_id"],
            "source_cell_id": move["source_cell_id"],
            "target_cell_id": move["target_cell_id"],
            **extra,
        }

    @router.post("/tenants/{tenant_id}/moves", status_code=201)
    async def start_move(tenant_id: TenantId, body: MoveRequest, actor: Actor) -> Move:
        with audit.record(
            "move.start", actor, tenant_id=tenant_id, target_cell_id=body.target_cell_id
        ) as event:
            move = await procedure.start(tenant_id, body.target_cell_id, actor)
            event.cell_id, event.target_cell_id = move["source_cell_id"], move["target_cell_id"]
            event.detail = recorded(move)
        return Move.model_validate(move)

    @router.get("/moves")
    def list_moves(tenant_id: str | None = None) -> MoveList:
        """Newest first."""
        moves = store.list_moves(tenant_id)
        return MoveList(items=[Move.model_validate(m) for m in moves])

    @router.get("/moves/{move_id}")
    def get_move(move_id: str) -> Move:
        return Move.model_validate(procedure.require(move_id))

    async def step(action: str, move_id: str, actor: str, run: Callable[[], Any]) -> Move:
        known = store.get_move(move_id)
        with audit.record(
            f"move.{action}",
            actor,
            tenant_id=known["tenant_id"] if known else None,
            cell_id=known["source_cell_id"] if known else None,
            target_cell_id=known["target_cell_id"] if known else None,
        ) as event:
            move = await run()
            event.detail = recorded(
                move,
                state=move["state"],
                cleanup_pending=move["cleanup_pending"],
                copied_rows=move["copied_rows"],
            )
        return Move.model_validate(move)

    @router.post("/moves/{move_id}/copy")
    async def copy_move(move_id: str, actor: Actor) -> Move:
        return await step("copy", move_id, actor, lambda: procedure.copy(move_id, actor))

    @router.post("/moves/{move_id}/switch")
    async def switch_move(move_id: str, actor: Actor) -> Move:
        return await step("switch", move_id, actor, lambda: procedure.switch(move_id, actor))

    @router.post("/moves/{move_id}/complete")
    async def complete_move(move_id: str, actor: Actor) -> Move:
        return await step("complete", move_id, actor, lambda: procedure.complete(move_id, actor))

    @router.post("/moves/{move_id}/rollback")
    async def rollback_move(
        move_id: str, actor: Actor, body: RollbackRequest | None = None
    ) -> Move:
        reason = body.reason if body else None
        return await step(
            "rollback", move_id, actor, lambda: procedure.rollback(move_id, reason, actor)
        )

    return router
