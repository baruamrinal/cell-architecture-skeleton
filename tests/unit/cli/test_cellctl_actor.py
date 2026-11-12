from __future__ import annotations

import httpx
import pytest

from cellarch.cli.cellctl import default_actor, main
from cellarch.common.headers import ACTOR_HEADER


def capture(argv: list[str]) -> httpx.Request:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://cp")
    assert main(argv, client=client, token="t") == 0
    return seen[0]


def test_sends_the_os_user_name_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELLCTL_ACTOR", raising=False)
    monkeypatch.setattr("cellarch.cli.cellctl.getpass.getuser", lambda: "lovin")
    assert capture(["tenants", "list"]).headers[ACTOR_HEADER] == "lovin"


def test_actor_from_environment_and_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELLCTL_ACTOR", "from-env")
    assert capture(["tenants", "list"]).headers[ACTOR_HEADER] == "from-env"
    assert capture(["--actor", "from-flag", "tenants", "list"]).headers[ACTOR_HEADER] == "from-flag"


@pytest.mark.parametrize(
    ("user", "actor"),
    [("John Smith", "John-Smith"), ("DOMAIN\\jsmith", "DOMAIN-jsmith"), ("x" * 80, "x" * 64)],
)
def test_default_actor_is_made_safe(
    monkeypatch: pytest.MonkeyPatch, user: str, actor: str
) -> None:
    monkeypatch.setattr("cellarch.cli.cellctl.getpass.getuser", lambda: user)
    assert default_actor() == actor


def test_no_actor_when_the_user_name_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def unknown() -> str:
        raise OSError("no user")

    monkeypatch.delenv("CELLCTL_ACTOR", raising=False)
    monkeypatch.setattr("cellarch.cli.cellctl.getpass.getuser", unknown)
    assert default_actor() is None
    assert ACTOR_HEADER not in capture(["tenants", "list"]).headers
