"""Stage 4 acceptance tests: mgr, the web UI's GraphQL backend in every cell (A77-A86)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cellarch.common.headers import TENANT_HEADER
from cellarch.common.sessions import SESSION_COOKIE
from cellarch.dev_idp.app import issue_token
from cellarch.mgr import contract
from cellarch.mgr.schema import breaking_changes, schema_sdl

from .conftest import ROUTE_CACHE_TTL, InProcessStacks, Platform
from .test_tenant_moves import onboard_on, two_cells
from .ui import (
    CANCEL_ORDER,
    CREATE_ORDER,
    CREATE_USER,
    DELETE_USER,
    ME,
    ORDER,
    USER,
    USERS,
    UiSession,
    data,
    error,
    mutate,
    query,
    sign_in,
)

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "src" / "cellarch" / "mgr" / "schema.graphql"


def new_user(platform: Platform, session: UiSession, email: str, key: str | None = None) -> str:
    created = data(
        mutate(platform, session, CREATE_USER, {"email": email, "name": "Ana", "key": key})
    )
    user_id: str = created["createUser"]["id"]
    return user_id


def new_order(platform: Platform, session: UiSession, user_id: str, key: str | None = None) -> str:
    created = data(mutate(platform, session, CREATE_ORDER, {"userId": user_id, "key": key}))
    order_id: str = created["createOrder"]["id"]
    return order_id


@pytest.fixture
def two_tenants(platform: Platform) -> tuple[UiSession, UiSession]:
    """acme on cell-1 and globex on cell-2, both signed in."""
    two_cells(platform)
    platform.onboard("acme")
    onboard_on(platform, "globex", "cell-2")
    return sign_in(platform, "acme"), sign_in(platform, "globex")


def test_a77_each_tenant_sees_and_changes_only_its_own_data(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, globex = two_tenants
    acme_user = new_user(platform, acme, "ana@acme.test")
    acme_order = new_order(platform, acme, acme_user)
    globex_user = new_user(platform, globex, "bo@globex.test")

    acme_page = data(query(platform, acme, USERS))["users"]
    globex_page = data(query(platform, globex, USERS))["users"]
    assert [u["id"] for u in acme_page["items"]] == [acme_user]
    assert [o["id"] for o in acme_page["items"][0]["orders"]] == [acme_order]
    assert [u["id"] for u in globex_page["items"]] == [globex_user]

    assert data(query(platform, globex, USER, {"id": acme_user})) == {"user": None}
    assert data(query(platform, globex, ORDER, {"id": acme_order})) == {"order": None}
    for document, variables in (
        (DELETE_USER, {"id": acme_user}),
        (CANCEL_ORDER, {"id": acme_order}),
        (CREATE_ORDER, {"userId": acme_user}),
    ):
        refused = mutate(platform, globex, document, variables)
        assert error(refused)["code"] in ("user_not_found", "order_not_found", "unknown_user")
    assert data(query(platform, acme, USER, {"id": acme_user}))["user"]["id"] == acme_user

    # Its own changes work: cancel the order, then delete the user.
    cancelled = data(mutate(platform, acme, CANCEL_ORDER, {"id": acme_order}))
    assert cancelled["cancelOrder"]["status"] == "cancelled"
    assert data(mutate(platform, acme, DELETE_USER, {"id": acme_user})) == {"deleteUser": True}
    assert data(query(platform, acme, USERS))["users"]["items"] == []


def test_a77_users_page_through_with_cursors(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, _ = two_tenants
    ids = [new_user(platform, acme, f"u{i}@acme.test") for i in range(3)]
    first = data(query(platform, acme, USERS, {"first": 2}))["users"]
    rest = data(query(platform, acme, USERS, {"first": 2, "after": first["nextCursor"]}))["users"]
    assert [u["id"] for u in first["items"] + rest["items"]] == ids
    assert rest["nextCursor"] is None


@pytest.mark.parametrize(
    ("session", "status", "code"),
    [
        ("none", 401, "unauthenticated"),
        ("expired", 401, "unauthenticated"),
        ("forged", 401, "unauthenticated"),
        ("garbage", 401, "unauthenticated"),
        ("another-tenants", 403, "session_tenant_mismatch"),
    ],
)
def test_a78_requests_without_a_valid_session_are_rejected(
    platform: Platform,
    two_tenants: tuple[UiSession, UiSession],
    session: str,
    status: int,
    code: str,
) -> None:
    _, globex = two_tenants
    token = {
        "none": None,
        "expired": issue_token(platform.idp_app, "acme", issued_at=time.time() - 10 * 3600),
        "forged": issue_token(platform.idp_app, "acme", forged=True),
        "garbage": "not-a-token",
        "another-tenants": globex.token,  # globex's session on acme's host
    }[session]
    cells_before = sum(len(c.requests) for c in platform.cells.values())

    response = query(platform, UiSession("acme", token), ME)

    assert response.status_code == status
    assert error(response)["code"] == code
    assert sum(len(c.requests) for c in platform.cells.values()) == cells_before  # no cell saw it


def test_a78_the_tenant_comes_from_the_session_only(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, _ = two_tenants
    response = query(platform, acme, ME, headers={TENANT_HEADER: "globex"})
    assert data(response)["me"] == {"tenantId": "acme", "cellId": "cell-1"}
    no_tenant_host = platform.idp.post("/auth/dev/sign-in", headers={"host": "localhost"})
    assert no_tenant_host.status_code == 400
    no_host = query(platform, UiSession("acme", acme.token), ME, headers={"host": "router"})
    assert (no_host.status_code, error(no_host)["code"]) == (400, "tenant_host_required")
    signed_out = platform.idp.post("/auth/sign-out")
    assert signed_out.cookies.get(SESSION_COOKIE) in (None, "")


def test_a79_a_cells_mgr_serves_only_the_tenants_it_hosts(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    misdirected = platform.cell_clients["cell-2"].get(
        "/graphql", params={"query": ME}, headers={TENANT_HEADER: "acme"}
    )
    assert misdirected.status_code == 421

    direct = platform.mgr_client("cell-1").get(
        "/graphql", params={"query": ME}, headers={TENANT_HEADER: "acme"}
    )
    assert direct.status_code in (401, 403)  # not from its gateway


def test_a79_a_stale_route_is_followed_to_the_new_cell(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, _ = two_tenants
    assert data(query(platform, acme, ME))["me"]["cellId"] == "cell-1"  # route cached
    platform.move("acme", "cell-2")
    assert data(query(platform, acme, ME))["me"]["cellId"] == "cell-2"


def test_a80_a_page_of_users_loads_their_orders_in_one_call(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, _ = two_tenants
    for i in range(3):
        new_order(platform, acme, new_user(platform, acme, f"u{i}@acme.test"))
    gateway = platform.cells["cell-1"]
    before = len(gateway.requests)

    page = data(query(platform, acme, USERS))["users"]

    calls = [r for r in gateway.requests[before:] if r.path == "/v1/orders"]
    assert len(calls) == 1
    assert all(len(u["orders"]) == 1 for u in page["items"])


def test_a81_a_rate_limited_tenant_gets_a_graphql_error_with_retry_after(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, _ = two_tenants
    platform.set_limit("acme", 0.5, 1)
    platform.clock.advance(ROUTE_CACHE_TTL + 1)
    assert query(platform, acme, ME).status_code == 200

    limited = query(platform, acme, ME)

    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    extensions = error(limited)
    assert (extensions["code"], extensions["status"]) == ("rate_limited", 429)
    assert extensions["retryAfter"] >= 1


def test_a82_a_retried_create_makes_no_duplicate(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, _ = two_tenants
    first = new_user(platform, acme, "ana@acme.test", key="ui-key-1")
    again = new_user(platform, acme, "ana@acme.test", key="ui-key-1")
    order = new_order(platform, acme, first, key="ui-key-2")
    order_again = new_order(platform, acme, first, key="ui-key-2")

    assert (again, order_again) == (first, order)
    page = data(query(platform, acme, USERS))["users"]
    assert [u["id"] for u in page["items"]] == [first]
    assert len(page["items"][0]["orders"]) == 1


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("too-deep", "query_too_deep"),
        ("too-costly", "query_too_costly"),
        ("mutation-over-get", "mutation_not_allowed"),
        ("no-csrf-header", "csrf_header_missing"),
    ],
)
def test_a83_abusive_or_unsafe_requests_are_refused(
    platform: Platform, two_tenants: tuple[UiSession, UiSession], case: str, code: str
) -> None:
    acme, _ = two_tenants
    if case == "too-deep":
        nested = "id " + "orders { user { " * 4 + "id" + " } }" * 4
        response = query(platform, acme, "query { users { items { " + nested + " } } }")
    elif case == "too-costly":
        response = query(
            platform, acme, "query { users(first: 100) { items { orders(first: 20) { id } } } }"
        )
    elif case == "mutation-over-get":
        response = query(platform, acme, CREATE_USER, {"email": "a@acme.test", "name": "A"})
    else:
        response = mutate(
            platform, acme, CREATE_USER, {"email": "a@acme.test", "name": "A"}, csrf=False
        )

    assert response.status_code in (400, 405)
    assert error(response)["code"] == code
    assert data(query(platform, acme, USERS))["users"]["items"] == []  # nothing ran


def test_a84_the_ui_keeps_working_while_the_control_plane_is_down(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, _ = two_tenants
    assert data(query(platform, acme, ME))["me"]["cellId"] == "cell-1"
    platform.control_plane.reachable = False
    platform.clock.advance(ROUTE_CACHE_TTL * 10)  # the cached route is stale now

    user = new_user(platform, acme, "ana@acme.test")

    assert [u["id"] for u in data(query(platform, acme, USERS))["users"]["items"]] == [user]


def test_a85_during_a_move_the_ui_is_read_only_then_served_by_the_new_cell(
    platform: Platform, two_tenants: tuple[UiSession, UiSession]
) -> None:
    acme, _ = two_tenants
    user = new_user(platform, acme, "ana@acme.test")
    seen: dict[str, object] = {}

    def half_way(cell_id: str, tenant_id: str) -> None:
        platform.clock.advance(ROUTE_CACHE_TTL + 1)  # the router sees `migrating`
        seen["read"] = query(platform, acme, USERS)
        seen["write"] = mutate(platform, acme, CREATE_USER, {"email": "x@acme.test", "name": "X"})

    platform.move("acme", "cell-2", InProcessStacks(platform, after_install=half_way))

    assert [u["id"] for u in data(seen["read"])["users"]["items"]] == [user]  # type: ignore[arg-type]
    write = seen["write"]
    assert write.status_code == 503  # type: ignore[attr-defined]
    assert error(write)["code"] == "tenant_migrating"  # type: ignore[arg-type]
    assert int(write.headers["Retry-After"]) >= 1  # type: ignore[attr-defined]
    platform.clock.advance(ROUTE_CACHE_TTL + 1)
    assert data(query(platform, acme, ME))["me"]["cellId"] == "cell-2"
    assert [u["id"] for u in data(query(platform, acme, USERS))["users"]["items"]] == [user]
    assert new_user(platform, acme, "bo@acme.test")  # writes work again


def test_a86_the_schema_and_the_v1_contract_are_checked() -> None:
    assert SCHEMA_FILE.read_text(encoding="utf-8") == schema_sdl(), (
        "src/cellarch/mgr/schema.graphql is out of date: run python -m cellarch.mgr.schema"
    )
    current = schema_sdl()
    removed_field = current.replace("  totalCents: Int!\n", "", 1)
    added_field = current.replace("type Order {\n", "type Order {\n  note: String\n", 1)
    assert removed_field != current and added_field != current
    assert breaking_changes(current, removed_field)  # a removed field breaks web-ui
    assert breaking_changes(current, added_field) == []  # an added field doesn't


def test_a86_mgr_uses_only_what_the_tenant_services_offer() -> None:
    missing = contract.unmet()
    assert missing == [], f"mgr relies on /v1 calls the tenant services don't offer: {missing}"
