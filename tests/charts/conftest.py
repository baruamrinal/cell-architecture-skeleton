"""Helpers for Helm chart tests: render a chart with `helm template` and parse the objects.

Skipped when Helm isn't installed (CI installs it with the k3d runtime).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CHARTS = Path(__file__).resolve().parents[2] / "charts"
HELM = shutil.which("helm")


class RenderError(Exception):
    """`helm template` failed (e.g. a required value is missing)."""


def render(chart: str, values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Render ``charts/<chart>`` with the given values (passed as --set-json); returns objects."""
    if HELM is None:  # pragma: no cover - guarded by the skip marker
        pytest.skip("helm is not installed")
    args = [HELM, "template", "test", str(CHARTS / chart), "--namespace", "default"]
    for key, value in _flatten(values or {}).items():
        args += ["--set-json", f"{key}={json.dumps(value)}"]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise RenderError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def lint(chart: str, values: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    if HELM is None:  # pragma: no cover - depends on the machine
        pytest.skip("helm is not installed")
    args = [HELM, "lint", str(CHARTS / chart), "--strict"]
    for key, value in _flatten(values).items():
        args += ["--set-json", f"{key}={json.dumps(value)}"]
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603


def _flatten(values: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def find(objects: list[dict[str, Any]], kind: str, name_part: str) -> dict[str, Any]:
    matches = [o for o in objects if o["kind"] == kind and name_part in o["metadata"]["name"]]
    assert len(matches) == 1, f"expected one {kind} matching {name_part!r}, got {len(matches)}"
    return matches[0]


def containers(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        c
        for o in objects
        if o["kind"] == "Deployment"
        for c in o["spec"]["template"]["spec"]["containers"]
    ]


def env(container: dict[str, Any]) -> dict[str, Any]:
    """Container env as {name: value or valueFrom}."""
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container.get("env", [])}
