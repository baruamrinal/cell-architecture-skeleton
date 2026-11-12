"""Keeps docs/acceptance-criteria.md and the acceptance tests in sync.

Every criterion ID in the document must have at least one acceptance test named
``test_<id>_...``, and every acceptance test ID must be documented.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRITERIA_DOC = ROOT / "docs" / "acceptance-criteria.md"
ACCEPTANCE_DIR = Path(__file__).resolve().parent

DOC_ID = re.compile(r"^\|\s*(A\d+[a-z]?)\s*\|", re.MULTILINE)
TEST_ID = re.compile(r"^def test_(a\d+[a-z]?)_", re.MULTILINE)


def documented_ids() -> list[str]:
    """Criterion IDs, normalised to upper case (A3b -> A3B) to match test names."""
    return [i.upper() for i in DOC_ID.findall(CRITERIA_DOC.read_text(encoding="utf-8"))]


def tested_ids() -> set[str]:
    ids: set[str] = set()
    for path in ACCEPTANCE_DIR.glob("test_*.py"):
        ids.update(match.upper() for match in TEST_ID.findall(path.read_text(encoding="utf-8")))
    return ids


def test_criteria_ids_are_unique() -> None:
    ids = documented_ids()
    assert ids, "no criteria found in docs/acceptance-criteria.md"
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate criterion IDs: {duplicates}"


def test_every_criterion_has_a_test() -> None:
    missing = sorted(set(documented_ids()) - tested_ids())
    assert not missing, f"criteria without an acceptance test: {missing}"


def test_every_acceptance_test_is_documented() -> None:
    undocumented = sorted(tested_ids() - set(documented_ids()))
    assert not undocumented, f"acceptance tests not in docs/acceptance-criteria.md: {undocumented}"
