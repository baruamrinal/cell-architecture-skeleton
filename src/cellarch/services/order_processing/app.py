"""order-processing service: the tenant's orders.

Owns the ``order_processing`` namespace of the tenant's database. Before placing an
order it asks **its own tenant's** user-account service whether the user exists, never
reading user-account's tables, and never calling another tenant's or cell's services.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, Query, Response
from pydantic import BaseModel, Field

from cellarch.common.config import require_env
from cellarch.common.db import utcnow
from cellarch.common.errors import ApiError
from cellarch.common.headers import INTERNAL_TOKEN_HEADER, TENANT_HEADER
from cellarch.common.pagination import DEFAULT_LIMIT, MAX_LIMIT, after_seq, page
from cellarch.common.sql import Row, open_database
from cellarch.services.common.app import create_service_app, tenant_api
from cellarch.services.common.idempotency import IDEMPOTENCY_HEADER, IdempotencyStore, idempotent
from cellarch.services.common.migrations import Migration, apply_migrations
from cellarch.services.common.settings import ServiceSettings

SERVICE = "order-processing"
NAMESPACE = "order_processing"
MIGRATIONS = [
    Migration(
        1,
        "create orders",
        "CREATE TABLE order_processing_orders ("
        "seq {seq_pk}, id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, status TEXT NOT NULL, lines TEXT NOT NULL, "
        "total_cents BIGINT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL); "
        "CREATE INDEX ix_order_processing_orders_user "
        "ON order_processing_orders (tenant_id, user_id, seq)",
    ),
]
RETRY_AFTER = {"Retry-After": "1"}


@dataclass(frozen=True)
class OrderSettings(ServiceSettings):
    user_account_url: str  # this tenant's user-account service

    @classmethod
    def from_env(cls) -> "OrderSettings":
        return cls(**cls.env_values(), user_account_url=require_env("USER_ACCOUNT_URL"))


class OrderLine(BaseModel):
    sku: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    quantity: int = Field(ge=1, le=1000)
    unit_price_cents: int = Field(ge=0, le=100_000_000)


class OrderCreate(BaseModel):
    user_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    lines: list[OrderLine] = Field(min_length=1, max_length=100)


class Order(BaseModel):
    id: str
    user_id: str
    status: Literal["placed", "cancelled"]
    lines: list[OrderLine]
    total_cents: int
    created_at: str
    updated_at: str


class OrderPage(BaseModel):
    items: list[Order]
    next_cursor: str | None


class DependencyUnavailableError(Exception):
    """The tenant's user-account service couldn't answer."""


class UserDirectory:
    """Asks the tenant's user-account service about users, with the stack credential."""

    def __init__(self, http: httpx.AsyncClient, settings: OrderSettings) -> None:
        self._http = http
        self._url = settings.user_account_url.rstrip("/")
        self._headers = {
            INTERNAL_TOKEN_HEADER: settings.stack_token,
            TENANT_HEADER: settings.tenant_id,
        }

    async def exists(self, user_id: str) -> bool:
        try:
            response = await self._http.get(
                f"{self._url}/v1/users/{quote(user_id, safe='')}", headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(str(exc)) from exc
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise DependencyUnavailableError(f"user-account answered HTTP {response.status_code}")


def _order(row: Row) -> Order:
    return Order.model_validate({**dict(row), "lines": json.loads(row["lines"])})


def create_app(
    settings: OrderSettings | None = None, *, http_client: httpx.AsyncClient | None = None
) -> FastAPI:
    settings = settings or OrderSettings.from_env()
    db, tenant = open_database(settings.database_url), settings.tenant_id
    apply_migrations(db, NAMESPACE, MIGRATIONS)
    idempotency = IdempotencyStore(db, NAMESPACE)
    http = http_client or httpx.AsyncClient(timeout=float(os.environ.get("CALL_TIMEOUT", "5")))
    users = UserDirectory(http, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if http_client is None:
            await http.aclose()

    app = create_service_app(SERVICE, settings, db, lifespan)
    api = tenant_api(settings)

    @api.post("/orders", status_code=201, response_model=Order)
    async def place_order(
        body: OrderCreate,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
    ) -> Response:
        async def handler() -> tuple[int, Any]:
            try:
                if not await users.exists(body.user_id):
                    raise ApiError(422, "unknown_user", f"user {body.user_id} does not exist")
            except DependencyUnavailableError as exc:
                raise ApiError(
                    503, "dependency_unavailable", "user-account is unavailable", RETRY_AFTER
                ) from exc
            order_id, now = f"ord_{uuid.uuid4().hex}", utcnow()
            total = sum(line.quantity * line.unit_price_cents for line in body.lines)
            lines = [line.model_dump() for line in body.lines]
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO order_processing_orders (id, tenant_id, user_id, status, lines, "
                    "total_cents, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'placed', ?, ?, ?, ?)",
                    (order_id, tenant, body.user_id, json.dumps(lines), total, now, now),
                )
            return 201, Order(
                id=order_id,
                user_id=body.user_id,
                status="placed",
                lines=body.lines,
                total_cents=total,
                created_at=now,
                updated_at=now,
            )

        return await idempotent(idempotency, idempotency_key, body.model_dump(), handler)

    @api.get("/orders")
    def list_orders(
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: str | None = None,
        user_id: str | None = None,
    ) -> OrderPage:
        query = "SELECT * FROM order_processing_orders WHERE tenant_id = ? AND seq > ?"
        params: list[Any] = [tenant, after_seq(cursor)]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        with db.connect() as conn:
            rows = conn.execute(f"{query} ORDER BY seq LIMIT ?", (*params, limit + 1)).fetchall()
        items, next_cursor = page(rows, limit)
        return OrderPage(items=[_order(r) for r in items], next_cursor=next_cursor)

    def find(order_id: str) -> Row:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM order_processing_orders WHERE tenant_id = ? AND id = ?",
                (tenant, order_id),
            ).fetchone()
        if row is None:
            raise ApiError(404, "order_not_found", "order not found")
        return row

    @api.get("/orders/{order_id}")
    def get_order(order_id: str) -> Order:
        return _order(find(order_id))

    @api.post("/orders/{order_id}/cancel")
    def cancel_order(order_id: str) -> Order:
        if find(order_id)["status"] == "cancelled":
            raise ApiError(409, "order_already_cancelled", "the order is already cancelled")
        with db.connect() as conn:
            conn.execute(
                "UPDATE order_processing_orders SET status = 'cancelled', updated_at = ? "
                "WHERE tenant_id = ? AND id = ?",
                (utcnow(), tenant, order_id),
            )
        return _order(find(order_id))

    app.include_router(api)
    return app
