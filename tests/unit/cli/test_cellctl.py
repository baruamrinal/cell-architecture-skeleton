from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.cli.cellctl import main
from cellarch.common.headers import INTERNAL_TOKEN_HEADER
from cellarch.control_plane.app import ControlPlaneSettings, create_app
from tests.fakes import async_client

from ..control_plane.test_cell_lifecycle_unit import fake_cells
from .test_onboarding import RecordingClusters

ADMIN = "admin-t"


@pytest.fixture
def control_plane(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"),
            admin_token=ADMIN,
            router_token="router-t",
            cell_credentials={"cell-1": "c1"},
        ),
        http_client=async_client(
            transport=httpx.MockTransport(
                fake_cells({"cell-1": "c1", "cell-3": "cell-3-credential-0123"})
            )
        ),
    )
    with TestClient(app, base_url="http://cp") as client:
        yield client


def run(client: TestClient, *args: str, token: str = ADMIN, clusters: object | None = None) -> int:
    toolkit = clusters if clusters is not None else RecordingClusters()
    return main([*args], client=client, token=token, clusters=toolkit)  # type: ignore[arg-type]


def test_register_and_list_cells(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(control_plane, "cells", "register", "cell-1", "http://cell-1", "--capacity", "8") == 0
    )
    registered = json.loads(capsys.readouterr().out)
    assert (registered["cell_id"], registered["capacity"]) == ("cell-1", 8)

    assert run(control_plane, "cells", "list") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == [
        "CELL",
        "TYPE",
        "STATUS",
        "TENANTS",
        "CAPACITY",
        "UTILIZATION",
        "CREATED",
        "BY",
        "URL",
    ]
    assert "admin:tester" in out
    assert "cell-1" in out and "0%" in out


def test_set_status_and_get_cell(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(control_plane, "cells", "register", "cell-1", "http://cell-1")
    assert run(control_plane, "cells", "set-status", "cell-1", "draining") == 0
    capsys.readouterr()
    assert run(control_plane, "cells", "get", "cell-1") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "draining"


def test_create_get_and_list_tenants(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(control_plane, "cells", "register", "cell-1", "http://cell-1")
    capsys.readouterr()

    assert (
        run(control_plane, "tenants", "create", "acme", "--name", "Acme", "--tier", "premium") == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert (created["cell_id"], created["tier"], created["status"]) == (
        "cell-1",
        "premium",
        "active",
    )

    assert run(control_plane, "tenants", "get", "acme") == 0
    assert json.loads(capsys.readouterr().out)["tenant_id"] == "acme"

    assert run(control_plane, "tenants", "list") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "TENANT",
        "NAME",
        "TIER",
        "CELL",
        "STATUS",
        "CREATED",
        "BY",
    ]
    assert lines[1].split() == [
        "acme",
        "Acme",
        "premium",
        "cell-1",
        "active",
        "admin:tester",
    ]


def test_create_installs_the_stack_and_delete_removes_it(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(control_plane, "cells", "register", "cell-1", "http://cell-1")
    clusters = RecordingClusters()
    assert run(control_plane, "tenants", "create", "globex", clusters=clusters) == 0
    assert clusters.calls[0][:4] == ("install", "cell-1", "tenant-globex", "tenant-stack")
    capsys.readouterr()
    assert run(control_plane, "tenants", "delete", "globex", clusters=clusters) == 0
    assert clusters.calls[-1] == ("uninstall", "cell-1", "tenant-globex")
    captured = capsys.readouterr()
    assert captured.out == "" and "deleted" in captured.err
    assert run(control_plane, "tenants", "get", "globex") == 1


def test_failed_onboarding_is_reported_and_undone(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(control_plane, "cells", "register", "cell-1", "http://cell-1")
    assert (
        run(
            control_plane, "tenants", "create", "globex", clusters=RecordingClusters(fail="install")
        )
        == 1
    )
    assert "helm install failed" in capsys.readouterr().err
    assert run(control_plane, "tenants", "get", "globex") == 1


def test_tenant_name_defaults_to_id(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(control_plane, "cells", "register", "cell-1", "http://cell-1")
    capsys.readouterr()
    assert run(control_plane, "tenants", "create", "globex") == 0
    assert json.loads(capsys.readouterr().out)["name"] == "globex"


def test_api_errors_go_to_stderr_with_exit_code_1(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(control_plane, "tenants", "get", "nobody") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tenant_not_found" in captured.err


def test_bad_credential_is_reported(
    control_plane: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(control_plane, "cells", "list", token="wrong") == 1
    assert "unauthenticated" in capsys.readouterr().err


def test_sends_token_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://cp")
    assert main(["tenants", "list"], client=client, token="tok") == 0
    assert seen[0].headers[INTERNAL_TOKEN_HEADER] == "tok"


def test_unreachable_control_plane(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://cp")
    assert main(["cells", "list"], client=client, token="t") == 1
    assert "cannot reach control plane" in capsys.readouterr().err


def test_usage_errors_exit_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["cells", "set-status", "cell-1", "bogus"], token="t")
    assert exc.value.code == 2


def test_requires_a_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CELLCTL_TOKEN", raising=False)
    assert main(["cells", "list"]) == 2
    assert "CELLCTL_TOKEN" in capsys.readouterr().err


def test_reads_url_and_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    monkeypatch.setenv("CELLCTL_URL", "http://cp.example:9000")
    monkeypatch.setenv("CELLCTL_TOKEN", "env-token")
    monkeypatch.setattr(
        "cellarch.cli.cellctl.make_client",
        lambda url: httpx.Client(transport=httpx.MockTransport(handler), base_url=url),
    )
    assert main(["cells", "list"]) == 0
    assert str(seen[0].url) == "http://cp.example:9000/v1/cells"
    assert seen[0].headers[INTERNAL_TOKEN_HEADER] == "env-token"


def test_make_client_targets_the_given_url() -> None:
    from cellarch.cli.api import make_client

    with make_client("http://cp.example:8000") as client:
        assert str(client.base_url) == "http://cp.example:8000"
