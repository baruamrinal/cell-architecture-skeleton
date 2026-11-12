from __future__ import annotations

import asyncio

import pytest
from starlette.types import Message, Receive, Scope, Send

from cellarch.data_plane.cell.app import CellSettings
from cellarch.data_plane.cell.concurrency import ConcurrencyLimit


async def ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def call(app: ConcurrencyLimit, path: str = "/v1/items", kind: str = "http") -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        messages.append(message)

    await app({"type": kind, "path": path, "headers": []}, receive, send)
    return messages


def status(messages: list[Message]) -> int:
    return int(messages[0]["status"])


def test_requests_under_the_limit_pass_through() -> None:
    limit = ConcurrencyLimit(ok_app, max_concurrent=1, cell_id="cell-x")
    assert status(asyncio.run(call(limit))) == 200
    assert status(asyncio.run(call(limit))) == 200  # the slot was released
    assert limit.in_flight == 0


def test_the_slot_is_released_when_the_app_fails() -> None:
    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    limit = ConcurrencyLimit(failing, max_concurrent=1, cell_id="cell-x")
    with pytest.raises(RuntimeError):
        asyncio.run(call(limit))
    assert limit.in_flight == 0


def test_non_http_traffic_is_not_limited() -> None:
    seen: list[str] = []

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    limit = ConcurrencyLimit(lifespan_app, max_concurrent=1, cell_id="cell-x")
    limit.in_flight = 1  # even when full
    asyncio.run(call(limit, kind="lifespan"))
    assert seen == ["lifespan"]


def test_max_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        ConcurrencyLimit(ok_app, max_concurrent=0, cell_id="cell-x")


def test_setting_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELL_ID", "cell-7")
    monkeypatch.setenv("CELL_INTERNAL_TOKEN", "t")
    monkeypatch.setenv("MAX_CONCURRENT_REQUESTS", "25")
    assert CellSettings.from_env().max_concurrent_requests == 25
    monkeypatch.delenv("MAX_CONCURRENT_REQUESTS")
    assert CellSettings.from_env().max_concurrent_requests == 100
