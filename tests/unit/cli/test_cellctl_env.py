"""cellctl reads settings from .env in the current folder (explicit settings win)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cellarch.cli.cellctl import main
from cellarch.common.headers import INTERNAL_TOKEN_HEADER


def write_env(text: str) -> None:
    Path(".env").write_text(text, encoding="utf-8")


def run_with_mock(argv: list[str], **kwargs: object) -> tuple[int, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://cp")
    return main(argv, client=client, **kwargs), seen  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def no_token_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CELLCTL_TOKEN", "CELLCTL_URL", "ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_uses_admin_token_from_env_file() -> None:
    write_env("ADMIN_TOKEN=from-dotenv-admin\n")
    code, seen = run_with_mock(["tenants", "list"])
    assert code == 0
    assert seen[0].headers[INTERNAL_TOKEN_HEADER] == "from-dotenv-admin"


def test_cellctl_token_in_env_file_wins_over_admin_token() -> None:
    write_env("ADMIN_TOKEN=admin\nCELLCTL_TOKEN=cellctl\n")
    _, seen = run_with_mock(["tenants", "list"])
    assert seen[0].headers[INTERNAL_TOKEN_HEADER] == "cellctl"


def test_environment_variable_wins_over_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    write_env("CELLCTL_TOKEN=from-file\n")
    monkeypatch.setenv("CELLCTL_TOKEN", "from-environment")
    _, seen = run_with_mock(["tenants", "list"])
    assert seen[0].headers[INTERNAL_TOKEN_HEADER] == "from-environment"


def test_flag_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    write_env("CELLCTL_TOKEN=from-file\n")
    monkeypatch.setenv("CELLCTL_TOKEN", "from-environment")
    _, seen = run_with_mock(["--token", "from-flag", "tenants", "list"])
    assert seen[0].headers[INTERNAL_TOKEN_HEADER] == "from-flag"


def test_url_from_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    write_env("ADMIN_TOKEN=t\nCELLCTL_URL=http://cp.example:9000\n")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        "cellarch.cli.cellctl.make_client",
        lambda url: httpx.Client(transport=httpx.MockTransport(handler), base_url=url),
    )
    assert main(["cells", "list"]) == 0
    assert str(seen[0].url) == "http://cp.example:9000/v1/cells"


def test_still_requires_a_token_without_env_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cells", "list"]) == 2
    err = capsys.readouterr().err
    assert "CELLCTL_TOKEN" in err and ".env" in err


def test_malformed_env_file_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    write_env("ADMIN_TOKEN=t\nnot valid\n")
    assert main(["cells", "list"]) == 2
    assert ".env line 2" in capsys.readouterr().err
