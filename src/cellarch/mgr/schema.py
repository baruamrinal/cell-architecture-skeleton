"""mgr's GraphQL schema: a tenant's users and orders (docs/plan.md §2.4).

Every resolver goes through the cell gateway's ``/v1`` API (gateway.py) for the
session's tenant. A user's orders are loaded with a DataLoader, so a page of users
gets all their orders in one batched ``/v1/orders`` call.

The schema changes additively (web-ui must work with every mgr version running during
a cell-by-cell rollout): ``schema.graphql`` holds the published schema, a test keeps it
in step with the code, and ``breaking_changes`` finds changes that would break it.
Regenerate it with ``python -m cellarch.mgr.schema > src/cellarch/mgr/schema.graphql``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import strawberry
from graphql import build_schema, find_breaking_changes
from strawberry.dataloader import DataLoader
from strawberry.types import Info

from cellarch.mgr.gateway import Gateway, GatewayError, segment

PAGE_SIZE, MAX_PAGE_SIZE = 20, 100
NESTED_ORDERS, MAX_NESTED_ORDERS = 5, 20
BATCH_LIMIT = 100  # orders fetched per batched call (the API's page limit)


@dataclass
class Context:
    gateway: Gateway
    tenant_id: str
    cell_id: str
    loaders: dict[str, DataLoader[Any, Any]] = field(default_factory=dict)


def _bounded(value: int, maximum: int, name: str) -> int:
    if not 1 <= value <= maximum:
        raise GatewayError(422, "invalid_argument", f"{name} must be between 1 and {maximum}")
    return value


# --- types ------------------------------------------------------------------------------


@strawberry.type
class OrderLine:
    sku: str
    quantity: int
    unit_price_cents: int


@strawberry.type
class Order:
    id: strawberry.ID
    user_id: strawberry.ID
    status: str
    lines: list[OrderLine]
    total_cents: int
    created_at: str
    updated_at: str

    @strawberry.field
    async def user(self, info: Info[Context, None]) -> User | None:
        loaded: User | None = await _loader(info.context, "users", _load_users).load(
            str(self.user_id)
        )
        return loaded


@strawberry.type
class User:
    id: strawberry.ID
    email: str
    name: str
    created_at: str

    @strawberry.field
    async def orders(self, info: Info[Context, None], first: int = NESTED_ORDERS) -> list[Order]:
        """The user's first orders, loaded together with the other users' on the page."""
        first = _bounded(first, MAX_NESTED_ORDERS, "first")
        orders: list[Order] = await _loader(info.context, "orders", _load_orders).load(
            (str(self.id), first)
        )
        return orders


@strawberry.type
class UserPage:
    items: list[User]
    next_cursor: str | None


@strawberry.type
class OrderPage:
    items: list[Order]
    next_cursor: str | None


@strawberry.type
class Me:
    tenant_id: str
    cell_id: str


@strawberry.input
class UserInput:
    email: str
    name: str


@strawberry.input
class OrderLineInput:
    sku: str
    quantity: int
    unit_price_cents: int


@strawberry.input
class OrderInput:
    user_id: strawberry.ID
    lines: list[OrderLineInput]


def _user(row: dict[str, Any]) -> User:
    return User(
        id=strawberry.ID(row["id"]),
        email=row["email"],
        name=row["name"],
        created_at=row["created_at"],
    )


def _order(row: dict[str, Any]) -> Order:
    return Order(
        id=strawberry.ID(row["id"]),
        user_id=strawberry.ID(row["user_id"]),
        status=row["status"],
        lines=[OrderLine(**line) for line in row["lines"]],
        total_cents=row["total_cents"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --- batched loaders --------------------------------------------------------------------


def _loader(context: Context, name: str, load: Any) -> DataLoader[Any, Any]:
    if name not in context.loaders:

        async def batch(keys: list[Any]) -> list[Any]:
            result: list[Any] = await load(context.gateway, keys)
            return result

        context.loaders[name] = DataLoader(load_fn=batch)
    return context.loaders[name]


async def _load_orders(gateway: Gateway, keys: list[tuple[str, int]]) -> list[list[Order]]:
    """One ``/v1/orders`` call for every user on the page; falls back per user only when
    that call can't hold everyone's first orders."""
    user_ids = sorted({user_id for user_id, _ in keys})
    page = await gateway.call(
        "GET", "/v1/orders", params=[("user_id", u) for u in user_ids] + [("limit", BATCH_LIMIT)]
    )
    found: dict[str, list[Order]] = defaultdict(list)
    for row in page["items"]:
        found[row["user_id"]].append(_order(row))
    results: list[list[Order]] = []
    for user_id, first in keys:
        orders = found[user_id]
        if page["next_cursor"] is not None and len(orders) < first:
            single = await gateway.call(
                "GET", "/v1/orders", params={"user_id": user_id, "limit": first}
            )
            orders = [_order(row) for row in single["items"]]
        results.append(orders[:first])
    return results


async def _load_users(gateway: Gateway, user_ids: list[str]) -> list[User | None]:
    rows = [
        await gateway.call("GET", f"/v1/users/{segment(u)}", not_found_is_none=True)
        for u in user_ids
    ]
    return [_user(row) if row else None for row in rows]


# --- operations -------------------------------------------------------------------------


@strawberry.type
class Query:
    @strawberry.field
    def me(self, info: Info[Context, None]) -> Me:
        return Me(tenant_id=info.context.tenant_id, cell_id=info.context.cell_id)

    @strawberry.field
    async def users(
        self, info: Info[Context, None], first: int = PAGE_SIZE, after: str | None = None
    ) -> UserPage:
        params: dict[str, Any] = {"limit": _bounded(first, MAX_PAGE_SIZE, "first")}
        if after:
            params["cursor"] = after
        page = await info.context.gateway.call("GET", "/v1/users", params=params)
        return UserPage(items=[_user(r) for r in page["items"]], next_cursor=page["next_cursor"])

    @strawberry.field
    async def user(self, info: Info[Context, None], id: strawberry.ID) -> User | None:
        row = await info.context.gateway.call(
            "GET", f"/v1/users/{segment(str(id))}", not_found_is_none=True
        )
        return _user(row) if row else None

    @strawberry.field
    async def orders(
        self,
        info: Info[Context, None],
        user_id: strawberry.ID | None = None,
        first: int = PAGE_SIZE,
        after: str | None = None,
    ) -> OrderPage:
        params: dict[str, Any] = {"limit": _bounded(first, MAX_PAGE_SIZE, "first")}
        if after:
            params["cursor"] = after
        if user_id:
            params["user_id"] = str(user_id)
        page = await info.context.gateway.call("GET", "/v1/orders", params=params)
        return OrderPage(items=[_order(r) for r in page["items"]], next_cursor=page["next_cursor"])

    @strawberry.field
    async def order(self, info: Info[Context, None], id: strawberry.ID) -> Order | None:
        row = await info.context.gateway.call(
            "GET", f"/v1/orders/{segment(str(id))}", not_found_is_none=True
        )
        return _order(row) if row else None


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_user(
        self, info: Info[Context, None], input: UserInput, idempotency_key: str | None = None
    ) -> User:
        row = await info.context.gateway.call(
            "POST",
            "/v1/users",
            json={"email": input.email, "name": input.name},
            idempotency_key=idempotency_key,
        )
        return _user(row)

    @strawberry.mutation
    async def delete_user(self, info: Info[Context, None], id: strawberry.ID) -> bool:
        await info.context.gateway.call("DELETE", f"/v1/users/{segment(str(id))}")
        return True

    @strawberry.mutation
    async def create_order(
        self, info: Info[Context, None], input: OrderInput, idempotency_key: str | None = None
    ) -> Order:
        body = {
            "user_id": str(input.user_id),
            "lines": [
                {"sku": x.sku, "quantity": x.quantity, "unit_price_cents": x.unit_price_cents}
                for x in input.lines
            ],
        }
        row = await info.context.gateway.call(
            "POST", "/v1/orders", json=body, idempotency_key=idempotency_key
        )
        return _order(row)

    @strawberry.mutation
    async def cancel_order(self, info: Info[Context, None], id: strawberry.ID) -> Order:
        row = await info.context.gateway.call("POST", f"/v1/orders/{segment(str(id))}/cancel")
        return _order(row)


schema = strawberry.Schema(query=Query, mutation=Mutation)


def schema_sdl() -> str:
    return schema.as_str() + "\n"


def breaking_changes(old_sdl: str, new_sdl: str) -> list[str]:
    """Changes from ``old_sdl`` to ``new_sdl`` that would break a client of the old one."""
    changes = find_breaking_changes(build_schema(old_sdl), build_schema(new_sdl))
    return [f"{c.type.name}: {c.description}" for c in changes]


if __name__ == "__main__":  # pragma: no cover - python -m cellarch.mgr.schema
    import sys

    sys.stdout.write(schema_sdl())
