"""Tenant placement strategies (docs/plan.md §2.2, decisions log).

Fill-first keeps the number of running cells low (cost): it fills the fullest
shared cell that stays at or under the target utilization after one more tenant.
When every eligible cell is past the target, it falls back to the emptiest cell
that still has room and flags the decision as over target, so the caller can warn.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class NoCapacityError(Exception):
    """No eligible cell has room for another tenant."""


@dataclass(frozen=True)
class CellCandidate:
    cell_id: str
    type: str
    status: str
    capacity: int
    tenant_count: int

    def utilization(self, extra: int = 0) -> float:
        return (self.tenant_count + extra) / self.capacity


@dataclass(frozen=True)
class PlacementDecision:
    cell_id: str
    over_target: bool


class PlacementStrategy(Protocol):
    def choose(self, cells: Sequence[CellCandidate]) -> PlacementDecision: ...


class FillFirstPlacement:
    def __init__(self, target_utilization: float = 0.75) -> None:
        if not 0 < target_utilization <= 1:
            raise ValueError("target_utilization must be in (0, 1]")
        self.target_utilization = target_utilization

    def choose(self, cells: Sequence[CellCandidate]) -> PlacementDecision:
        eligible = [
            c
            for c in cells
            if c.type == "shared" and c.status == "active" and c.tenant_count < c.capacity
        ]
        if not eligible:
            raise NoCapacityError("no shared active cell has room")

        under_target = [c for c in eligible if c.utilization(extra=1) <= self.target_utilization]
        if under_target:
            # Fullest first; ties broken by cell id for determinism.
            best = min(under_target, key=lambda c: (-c.utilization(), c.cell_id))
            return PlacementDecision(best.cell_id, over_target=False)

        emptiest = min(eligible, key=lambda c: (c.utilization(), c.cell_id))
        return PlacementDecision(emptiest.cell_id, over_target=True)
