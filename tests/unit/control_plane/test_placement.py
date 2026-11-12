from __future__ import annotations

from dataclasses import replace

import pytest

from cellarch.control_plane.placement import CellCandidate, FillFirstPlacement, NoCapacityError


def cell(cell_id: str, tenants: int, capacity: int = 4, **overrides: object) -> CellCandidate:
    base = CellCandidate(
        cell_id=cell_id, type="shared", status="active", capacity=capacity, tenant_count=tenants
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


placement = FillFirstPlacement(target_utilization=0.75)


def test_prefers_the_fullest_cell_still_under_target() -> None:
    decision = placement.choose([cell("cell-1", 1), cell("cell-2", 2), cell("cell-3", 0)])
    assert decision.cell_id == "cell-2"
    assert not decision.over_target


def test_a_cell_at_target_is_skipped() -> None:
    # cell-1 has 3 of 4: one more would be 100% > 75%.
    decision = placement.choose([cell("cell-1", 3), cell("cell-2", 0)])
    assert decision.cell_id == "cell-2"


def test_ties_break_by_cell_id() -> None:
    assert placement.choose([cell("cell-b", 0), cell("cell-a", 0)]).cell_id == "cell-a"


def test_utilization_not_count_decides_fullest() -> None:
    # 2/10 = 20% vs 1/2 = 50%; cell-small is fuller.
    cells = [cell("cell-big", 2, capacity=10), cell("cell-small", 1, capacity=4)]
    assert placement.choose(cells).cell_id == "cell-small"


def test_past_target_falls_back_to_emptiest_cell_with_room() -> None:
    decision = placement.choose([cell("cell-1", 3), cell("cell-2", 3, capacity=8)])
    # both at/over target for one more? cell-2: 4/8 = 50% is under target.
    assert decision.cell_id == "cell-2"
    assert not decision.over_target

    decision = placement.choose([cell("cell-1", 3), cell("cell-2", 3, capacity=5)])
    # cell-1 -> 4/4 = 100%, cell-2 -> 4/5 = 80%: both over target; emptiest is cell-2.
    assert decision.cell_id == "cell-2"
    assert decision.over_target


@pytest.mark.parametrize(
    "excluded",
    [
        {"status": "draining"},
        {"status": "disabled"},
        {"type": "dedicated"},
    ],
)
def test_ineligible_cells_are_never_chosen(excluded: dict[str, str]) -> None:
    decision = placement.choose([cell("cell-1", 0, **excluded), cell("cell-2", 3)])
    assert decision.cell_id == "cell-2"


def test_full_cells_are_never_chosen() -> None:
    with pytest.raises(NoCapacityError):
        placement.choose([cell("cell-1", 4), cell("cell-2", 4)])


def test_no_cells_raises() -> None:
    with pytest.raises(NoCapacityError):
        placement.choose([])


@pytest.mark.parametrize("target", [0, -0.1, 1.01])
def test_target_must_be_a_fraction(target: float) -> None:
    with pytest.raises(ValueError, match="target_utilization"):
        FillFirstPlacement(target_utilization=target)
