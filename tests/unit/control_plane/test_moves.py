"""The control plane's move procedure and audit log, against scripted cells.

The happy paths run end to end in tests/acceptance/test_tenant_moves.py; these cover
the states, the errors cells can answer with, and the bookkeeping around them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.common.headers import INTERNAL_TOKEN_HEADER
from cellarch.control_plane.app import ControlPlaneSettings, create_app
from cellarch.control_plane.store import ControlPlaneStore

ADMIN = {INTERNAL_TOKEN_HEADER: "admin-t"}


class ScriptedCells:
    """Every cell's internal API. ``answers`` overrides a call: (method, cell, step)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.answers: dict[tuple[str, str, str], httpx.Response | Exception] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        cell = request.url.host
        parts = request.url.path.strip("/").split("/")  # internal/tenants[/id[/step]]
        step = parts[3] if len(parts) > 3 else ("tenant" if len(parts) > 2 else "tenants")
        key = (request.method, cell, step)
        self.calls.append(key)
        answer = self.answers.get(key)
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "cell_id": cell})
        if step == "export":
            lines = [{"kind": "header"}, {"kind": "end", "counts": {}}]
            return httpx.Response(
                200, content=b"".join(json.dumps(x).encode() + b"\n" for x in lines)
            )
        if step == "import":
            return httpx.Response(200, json={"tables": {"user_account_users": 2, "orders": 1}})
        if request.method == "DELETE":
            return httpx.Response(204)
        if step == "tenants" and request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200, json={"tenant_id": "acme", "status": "active", "provisioned_at": "x"}
        )


def cell_error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": code, "message": "scripted"})


@pytest.fixture
def cells() -> ScriptedCells:
    return ScriptedCells()


@pytest.fixture
def admin(tmp_path: Path, cells: ScriptedCells) -> Iterator[TestClient]:
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"),
            admin_token="admin-t",
            router_token="router-t",
            cell_credentials={f"cell-{i}": f"cell-{i}-credential" for i in (1, 2, 3)},
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(cells.handler)),
    )
    with TestClient(app, headers=ADMIN) as client:
        for cell_id in ("cell-1", "cell-2"):
            registered = client.post(
                "/v1/cells", json={"cell_id": cell_id, "url": f"http://{cell_id}", "capacity": 4}
            )
            assert registered.status_code == 201, registered.text
        assert (
            client.post("/v1/tenants", json={"tenant_id": "acme", "name": "A"}).status_code == 201
        )
        assert client.post("/v1/tenants/acme/activate").status_code == 200
        yield client


def start(admin: TestClient, target: str | None = "cell-2") -> httpx.Response:
    body = {"target_cell_id": target} if target else {}
    response: httpx.Response = admin.post("/v1/tenants/acme/moves", json=body)
    return response


def step(admin: TestClient, move: dict[str, Any], name: str, **kw: Any) -> httpx.Response:
    response: httpx.Response = admin.post(f"/v1/moves/{move['move_id']}/{name}", **kw)
    return response


def tenant(admin: TestClient) -> dict[str, Any]:
    result: dict[str, Any] = admin.get("/v1/tenants/acme").json()
    return result


class TestStart:
    def test_freezes_the_source_and_prepares_the_target(
        self, admin: TestClient, cells: ScriptedCells
    ) -> None:
        response = start(admin)
        assert response.status_code == 201, response.text
        move = response.json()
        assert move["move_id"].startswith("mv_")
        assert (move["state"], move["source_cell_id"], move["target_cell_id"]) == (
            "started",
            "cell-1",
            "cell-2",
        )
        assert move["created_by"] == "admin"
        assert ("POST", "cell-1", "freeze") in cells.calls
        assert ("POST", "cell-2", "tenants") in cells.calls
        assert tenant(admin)["status"] == "migrating"
        assert admin.get(f"/v1/moves/{move['move_id']}").json() == move
        assert admin.get("/v1/cells/cell-2").json()["incoming_tenants"] == 1

    def test_without_a_target_placement_chooses_another_cell(self, admin: TestClient) -> None:
        response = start(admin, target=None)
        assert (response.status_code, response.json()["target_cell_id"]) == (201, "cell-2")

    def test_without_a_target_and_no_room_elsewhere(self, admin: TestClient) -> None:
        admin.patch("/v1/cells/cell-2", json={"status": "draining"})
        response = start(admin, target=None)
        assert (response.status_code, response.json()["error"]) == (503, "no_capacity")

    def test_an_unknown_tenant(self, admin: TestClient) -> None:
        response = admin.post("/v1/tenants/nobody/moves", json={"target_cell_id": "cell-2"})
        assert (response.status_code, response.json()["error"]) == (404, "tenant_not_found")

    @pytest.mark.parametrize("failing", ["freeze", "provision"])
    def test_a_cell_failing_at_the_start_is_undone(
        self, admin: TestClient, cells: ScriptedCells, failing: str
    ) -> None:
        key = ("POST", "cell-1", "freeze") if failing == "freeze" else ("POST", "cell-2", "tenants")
        cells.answers[key] = httpx.ConnectError("down")
        response = start(admin)
        assert (response.status_code, response.json()["error"]) == (502, "cell_unavailable")
        assert tenant(admin)["status"] == "active"
        [move] = admin.get("/v1/moves").json()["items"]
        assert (move["state"], move["error"]) == ("rolled_back", "cell_unavailable")
        if failing == "provision":
            assert ("POST", "cell-1", "unfreeze") in cells.calls
            assert ("DELETE", "cell-2", "tenant") in cells.calls


class TestSteps:
    @pytest.fixture
    def move(self, admin: TestClient) -> dict[str, Any]:
        response = start(admin)
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    def test_the_whole_way(
        self, admin: TestClient, cells: ScriptedCells, move: dict[str, Any]
    ) -> None:
        copied = step(admin, move, "copy").json()
        assert (copied["state"], copied["copied_rows"]) == ("copied", 3)
        switched = step(admin, move, "switch").json()
        assert switched["state"] == "switched"
        assert (tenant(admin)["cell_id"], tenant(admin)["status"]) == ("cell-2", "active")
        assert step(admin, move, "switch").json()["state"] == "switched"  # idempotent
        done = step(admin, move, "complete").json()
        assert (done["state"], done["cleanup_pending"]) == ("completed", None)
        assert ("DELETE", "cell-1", "tenant") in cells.calls
        assert step(admin, move, "complete").json() == done  # nothing left to do
        assert step(admin, move, "copy").json() == done  # a later step already ran

    @pytest.mark.parametrize(
        ("answer", "status", "error"),
        [
            (cell_error(409, "tenant_stack_not_ready"), 409, "tenant_stack_not_ready"),
            (cell_error(409, "tenant_data_not_empty"), 409, "tenant_data_not_empty"),
            (cell_error(422, "invalid_export"), 422, "invalid_export"),
            (cell_error(500, "boom"), 502, "cell_unavailable"),
            (httpx.ConnectError("down"), 502, "cell_unavailable"),
        ],
    )
    def test_copy_errors_leave_the_move_where_it_was(
        self,
        admin: TestClient,
        cells: ScriptedCells,
        move: dict[str, Any],
        answer: Any,
        status: int,
        error: str,
    ) -> None:
        cells.answers[("POST", "cell-2", "import")] = answer
        response = step(admin, move, "copy")
        assert (response.status_code, response.json()["error"]) == (status, error)
        assert admin.get(f"/v1/moves/{move['move_id']}").json()["state"] == "started"

    def test_an_export_the_source_refuses(
        self, admin: TestClient, cells: ScriptedCells, move: dict[str, Any]
    ) -> None:
        cells.answers[("GET", "cell-1", "export")] = cell_error(409, "tenant_not_frozen")
        response = step(admin, move, "copy")
        assert (response.status_code, response.json()["error"]) == (409, "tenant_not_frozen")

    @pytest.mark.parametrize(
        ("answer", "status", "error"),
        [
            (cell_error(409, "tenant_stack_not_ready"), 409, "tenant_stack_not_ready"),
            (httpx.ConnectError("down"), 502, "cell_unavailable"),
        ],
    )
    def test_switch_needs_the_targets_stack_ready(
        self,
        admin: TestClient,
        cells: ScriptedCells,
        move: dict[str, Any],
        answer: Any,
        status: int,
        error: str,
    ) -> None:
        step(admin, move, "copy")
        cells.answers[("POST", "cell-2", "activate")] = answer
        response = step(admin, move, "switch")
        assert (response.status_code, response.json()["error"]) == (status, error)
        assert tenant(admin)["cell_id"] == "cell-1"

    def test_steps_out_of_order_are_refused(self, admin: TestClient, move: dict[str, Any]) -> None:
        for name in ("switch", "complete"):
            response = step(admin, move, name)
            assert (response.status_code, response.json()["error"]) == (409, "move_state_conflict")

    def test_an_unknown_move(self, admin: TestClient) -> None:
        for path in ("/v1/moves/mv_nope", "/v1/moves/mv_nope/copy"):
            call = admin.get if path.count("/") == 3 else admin.post
            response = call(path)
            assert (response.status_code, response.json()["error"]) == (404, "move_not_found")

    def test_rollback_unfreezes_and_clears_the_target(
        self, admin: TestClient, cells: ScriptedCells, move: dict[str, Any]
    ) -> None:
        step(admin, move, "copy")
        back = step(admin, move, "rollback", json={"reason": "tenant_stack_not_ready"}).json()
        assert (back["state"], back["error"], back["cleanup_pending"]) == (
            "rolled_back",
            "tenant_stack_not_ready",
            None,
        )
        assert (tenant(admin)["cell_id"], tenant(admin)["status"]) == ("cell-1", "active")
        assert ("POST", "cell-1", "unfreeze") in cells.calls
        assert ("DELETE", "cell-2", "tenant") in cells.calls
        assert step(admin, move, "rollback").json() == back  # idempotent
        assert admin.get("/v1/cells/cell-2").json()["incoming_tenants"] == 0

    def test_rollback_flags_a_target_it_could_not_clear(
        self, admin: TestClient, cells: ScriptedCells, move: dict[str, Any]
    ) -> None:
        cells.answers[("DELETE", "cell-2", "tenant")] = httpx.ConnectError("down")
        back = step(admin, move, "rollback").json()
        assert (back["state"], back["cleanup_pending"], back["error"]) == (
            "rolled_back",
            "cell-2",
            "rolled_back",
        )

    def test_rollback_waits_for_the_source(
        self, admin: TestClient, cells: ScriptedCells, move: dict[str, Any]
    ) -> None:
        cells.answers[("POST", "cell-1", "unfreeze")] = httpx.ConnectError("down")
        response = step(admin, move, "rollback")
        assert (response.status_code, response.json()["error"]) == (502, "cell_unavailable")
        assert tenant(admin)["status"] == "migrating"  # retry once the source is back

    def test_no_rollback_after_the_switch(self, admin: TestClient, move: dict[str, Any]) -> None:
        step(admin, move, "copy")
        step(admin, move, "switch")
        response = step(admin, move, "rollback")
        assert (response.status_code, response.json()["error"]) == (409, "move_state_conflict")

    def test_a_moving_tenant_cannot_be_deleted(
        self, admin: TestClient, move: dict[str, Any]
    ) -> None:
        response = admin.delete("/v1/tenants/acme")
        assert (response.status_code, response.json()["error"]) == (409, "move_in_progress")

    def test_a_cell_with_incoming_tenants_cannot_be_decommissioned(
        self, admin: TestClient, move: dict[str, Any]
    ) -> None:
        response = admin.delete("/v1/cells/cell-2")
        assert (response.status_code, response.json()["error"]) == (409, "cell_not_empty")

    def test_incoming_tenants_count_against_the_targets_room(
        self, admin: TestClient, move: dict[str, Any]
    ) -> None:
        admin.patch("/v1/cells/cell-2", json={"capacity": 1})
        assert (
            admin.post("/v1/tenants", json={"tenant_id": "globex", "name": "G"}).json()["cell_id"]
            == "cell-1"
        )

    def test_moves_list_newest_first_and_by_tenant(
        self, admin: TestClient, move: dict[str, Any]
    ) -> None:
        assert [m["move_id"] for m in admin.get("/v1/moves").json()["items"]] == [move["move_id"]]
        assert admin.get("/v1/moves", params={"tenant_id": "globex"}).json()["items"] == []


class TestAudit:
    def test_records_who_did_what(self, admin: TestClient) -> None:
        admin.post(
            "/v1/tenants/acme/moves", json={"target_cell_id": "cell-1"}, headers={"X-Actor": "ana"}
        )
        [event] = admin.get("/v1/audit", params={"limit": 1}).json()["items"]
        assert event["actor"] == "admin:ana"
        assert (event["action"], event["outcome"], event["error"]) == (
            "move.start",
            "refused",
            "same_cell",
        )
        assert (event["tenant_id"], event["target_cell_id"]) == ("acme", "cell-1")

    def test_failures_are_recorded_as_failed(self, admin: TestClient, cells: ScriptedCells) -> None:
        cells.answers[("POST", "cell-1", "freeze")] = httpx.ConnectError("down")
        start(admin)
        [event] = admin.get("/v1/audit", params={"limit": 1}).json()["items"]
        assert (event["action"], event["outcome"]) == ("move.start", "failed")

    def test_unexpected_errors_are_recorded_too(
        self, admin: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store: ControlPlaneStore = admin.app.state.store  # type: ignore[attr-defined]

        def broken(*args: object, **kwargs: object) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(store, "set_rate_limit", broken)
        with pytest.raises(RuntimeError):
            admin.put("/v1/tenants/acme/rate-limit", json={"rps": 1, "burst": 1})
        [event] = admin.get("/v1/audit", params={"limit": 1}).json()["items"]
        assert (event["action"], event["outcome"], event["error"]) == (
            "tenant.rate_limit.set",
            "failed",
            "internal_error",
        )

    def test_cell_changes(self, admin: TestClient) -> None:
        admin.patch("/v1/cells/cell-2", json={"capacity": 9})
        admin.delete("/v1/cells/cell-2")
        admin.delete("/v1/tenants/acme/rate-limit")
        events = admin.get("/v1/audit", params={"limit": 3}).json()["items"]
        assert [(e["action"], e["cell_id"] or e["tenant_id"]) for e in events] == [
            ("tenant.rate_limit.clear", "acme"),
            ("cell.decommission", "cell-2"),
            ("cell.update", "cell-2"),
        ]
        assert events[2]["detail"] == {"capacity": 9}

    @pytest.mark.parametrize("cursor", ["nope", "eyJhZnRlciI6LTF9"])
    def test_bad_cursors_are_refused(self, admin: TestClient, cursor: str) -> None:
        response = admin.get("/v1/audit", params={"cursor": cursor})
        assert (response.status_code, response.json()["error"]) == (400, "invalid_cursor")

    def test_needs_the_admin_credential(self, admin: TestClient) -> None:
        response = admin.get("/v1/audit", headers={INTERNAL_TOKEN_HEADER: "router-t"})
        assert response.status_code == 403


class TestCellAnswers:
    @pytest.fixture
    def move(self, admin: TestClient) -> dict[str, Any]:
        result: dict[str, Any] = start(admin).json()
        return result

    def test_a_refusal_without_the_error_shape_is_unavailable(
        self, admin: TestClient, cells: ScriptedCells, move: dict[str, Any]
    ) -> None:
        cells.answers[("POST", "cell-2", "import")] = httpx.Response(400, text="bad gateway page")
        response = step(admin, move, "copy")
        assert (response.status_code, response.json()["error"]) == (502, "cell_unavailable")

    def test_switch_with_another_refusal_is_unavailable(
        self, admin: TestClient, cells: ScriptedCells, move: dict[str, Any]
    ) -> None:
        step(admin, move, "copy")
        cells.answers[("POST", "cell-2", "activate")] = cell_error(404, "tenant_not_found")
        response = step(admin, move, "switch")
        assert (response.status_code, response.json()["error"]) == (502, "cell_unavailable")

    def test_complete_before_the_switch_is_refused(
        self, admin: TestClient, move: dict[str, Any]
    ) -> None:
        step(admin, move, "copy")
        response = step(admin, move, "complete")
        assert (response.status_code, response.json()["error"]) == (409, "move_state_conflict")

    def test_a_moving_tenant_is_not_activated_again(
        self, admin: TestClient, move: dict[str, Any]
    ) -> None:
        response = admin.post("/v1/tenants/acme/activate")
        assert (response.status_code, response.json()["error"]) == (409, "move_in_progress")
