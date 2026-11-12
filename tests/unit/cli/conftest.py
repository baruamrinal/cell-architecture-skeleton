from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def fixed_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the caller name cellctl sends independent of who runs the tests."""
    monkeypatch.setenv("CELLCTL_ACTOR", "tester")
