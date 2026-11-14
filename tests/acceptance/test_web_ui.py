"""Stage 4 acceptance tests: the tenant web UI (A87-A88)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.charts.conftest import render

ROOT = Path(__file__).resolve().parents[2]
WEB_UI = ROOT / "web-ui"
PLATFORM = {
    "secrets": {
        "adminToken": "admin-secret-value",
        "routerToken": "router-secret-value",
        "metricsToken": "metrics-secret-value",
        "cellCredentials": {"cell-1": "cell-1-secret-value"},
    },
    "edge": {"webUiUrl": "http://web-ui:8080"},
}


def _routes(config: str) -> dict[str, str]:
    lines = [line.strip() for line in config.splitlines()]
    return {
        line.split()[1]: following
        for line, following in zip(lines, lines[1:], strict=False)
        if line.startswith("location ")
    }


@pytest.mark.charts
def test_a87_a_tenants_address_serves_the_ui_and_its_api() -> None:
    edge = next(
        o
        for o in render("platform", PLATFORM)
        if o["kind"] == "ConfigMap" and o["metadata"]["name"].endswith("edge")
    )
    config = edge["data"]["default.conf"]
    routes = _routes(config)
    assert "http://web-ui:8080" in routes["/"]
    for path in ("/graphql", "/v1/", "/_router/"):
        assert "router" in routes[path], path
    assert "dev-idp" in routes["/auth/"]
    assert "proxy_set_header Host $host" in config  # the tenant comes from the address

    web_ui = render("web-ui", {"image": {"tag": "0.1.0-abc1234"}})
    policy = next(o for o in web_ui if o["kind"] == "NetworkPolicy")
    assert policy["spec"]["egress"] == []  # static files: no outbound calls
    (rule,) = policy["spec"]["ingress"]
    assert rule["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "edge"


def _corepack() -> str | None:
    found = shutil.which("corepack")
    if found:
        return found
    node = shutil.which("node")
    if node:  # some shells expose node but not the corepack shim
        script = Path(node).parent / "node_modules" / "corepack" / "dist" / "corepack.js"
        if script.is_file():
            return f"{node}|{script}"
    return None


@pytest.mark.frontend
def test_a88_the_ui_screens_show_the_tenants_data_and_run_its_changes() -> None:
    corepack = _corepack()
    if corepack is None:
        pytest.skip("Node 24 with Corepack is not installed (see README: web UI)")
    if not (WEB_UI / "node_modules").is_dir():
        pytest.skip("web-ui packages are not installed: run `corepack pnpm install` in web-ui/")
    command = corepack.split("|") if "|" in corepack else [corepack]
    result = subprocess.run(  # noqa: S603 - fixed command
        [*command, "pnpm", "exec", "vitest", "run"],
        cwd=WEB_UI,
        env={**os.environ, "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", "CI": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "failed" not in result.stdout.lower().split("tests")[-1]
