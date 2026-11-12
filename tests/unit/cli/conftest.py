from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def fixed_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the caller name cellctl sends independent of who runs the tests."""
    monkeypatch.setenv("CELLCTL_ACTOR", "tester")


@pytest.fixture(autouse=True)
def isolated_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run in an empty folder, so a developer's real .env never leaks into the tests."""
    monkeypatch.chdir(tmp_path)
