from __future__ import annotations

import json

import httpx
import pytest

from cellarch.cli.cellctl import main


def capture(argv: list[str]) -> tuple[int, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"tenant_id": "acme", "rate_limit": {"rps": 2}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://cp")
    return main(argv, client=client, token="t"), seen


def test_set_limit(capsys: pytest.CaptureFixture[str]) -> None:
    code, seen = capture(["tenants", "set-limit", "acme", "--rps", "2", "--burst", "4"])
    assert code == 0
    assert (seen[0].method, seen[0].url.path) == ("PUT", "/v1/tenants/acme/rate-limit")
    assert json.loads(seen[0].content) == {"rps": 2.0, "burst": 4}
    assert json.loads(capsys.readouterr().out)["rate_limit"] == {"rps": 2}


def test_clear_limit() -> None:
    code, seen = capture(["tenants", "set-limit", "acme", "--clear"])
    assert code == 0
    assert (seen[0].method, seen[0].url.path) == ("DELETE", "/v1/tenants/acme/rate-limit")


@pytest.mark.parametrize(
    "args",
    [
        ["tenants", "set-limit", "acme"],
        ["tenants", "set-limit", "acme", "--rps", "2"],
        ["tenants", "set-limit", "acme", "--clear", "--rps", "2", "--burst", "4"],
        ["tenants", "set-limit", "acme", "--rps", "0", "--burst", "4"],
    ],
)
def test_set_limit_usage_errors(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        capture(args)
    assert exc.value.code == 2
