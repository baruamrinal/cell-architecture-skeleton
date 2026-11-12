"""user-account service: the tenant's users.

Owns the ``user_account`` namespace of the tenant's database. Other services never read
its tables; order-processing asks this API whether a user exists.
"""

import sqlite3
import uuid
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Response
from pydantic import BaseModel, Field

from cellarch.common.db import connect, utcnow
from cellarch.common.errors import ApiError
from cellarch.services.common.app import create_service_app, tenant_api
from cellarch.services.common.idempotency import IDEMPOTENCY_HEADER, IdempotencyStore, idempotent
from cellarch.services.common.migrations import Migration, apply_migrations
from cellarch.services.common.pagination import DEFAULT_LIMIT, MAX_LIMIT, after_seq, page
from cellarch.services.common.settings import ServiceSettings

SERVICE = "user-account"
NAMESPACE = "user_account"
MIGRATIONS = [
    Migration(
        1,
        "create users",
        "CREATE TABLE user_account_users ("
        "id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT NOT NULL, "
        "name TEXT NOT NULL, created_at TEXT NOT NULL, seq INTEGER NOT NULL UNIQUE); "
        "CREATE UNIQUE INDEX ux_user_account_users_email ON user_account_users (tenant_id, email)",
    ),
]
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class UserCreate(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    name: str = Field(min_length=1, max_length=200)


class User(BaseModel):
    id: str
    email: str
    name: str
    created_at: str


class UserPage(BaseModel):
    items: list[User]
    next_cursor: str | None


def _user(row: sqlite3.Row) -> User:
    return User(id=row["id"], email=row["email"], name=row["name"], created_at=row["created_at"])


def create_app(settings: ServiceSettings | None = None) -> FastAPI:
    settings = settings or ServiceSettings.from_env()
    apply_migrations(settings.db_path, NAMESPACE, MIGRATIONS)
    idempotency = IdempotencyStore(settings.db_path, NAMESPACE)
    db, tenant = settings.db_path, settings.tenant_id

    app = create_service_app(SERVICE, settings)
    api = tenant_api(settings)

    @api.post("/users", status_code=201, response_model=User)
    async def create_user(
        body: UserCreate,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
    ) -> Response:
        async def handler() -> tuple[int, Any]:
            user_id, email, now = f"usr_{uuid.uuid4().hex}", body.email.lower(), utcnow()
            try:
                with connect(db) as conn:
                    conn.execute(
                        "INSERT INTO user_account_users "
                        "(id, tenant_id, email, name, created_at, seq) VALUES (?, ?, ?, ?, ?, "
                        "(SELECT COALESCE(MAX(seq), 0) + 1 FROM user_account_users))",
                        (user_id, tenant, email, body.name, now),
                    )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "email_exists", "a user with this email exists") from exc
            return 201, User(id=user_id, email=email, name=body.name, created_at=now)

        return await idempotent(idempotency, idempotency_key, body.model_dump(), handler)

    @api.get("/users")
    def list_users(
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> UserPage:
        with connect(db) as conn:
            rows = conn.execute(
                "SELECT * FROM user_account_users WHERE tenant_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (tenant, after_seq(cursor), limit + 1),
            ).fetchall()
        items, next_cursor = page(rows, limit)
        return UserPage(items=[_user(r) for r in items], next_cursor=next_cursor)

    def find(user_id: str) -> sqlite3.Row:
        with connect(db) as conn:
            row: sqlite3.Row | None = conn.execute(
                "SELECT * FROM user_account_users WHERE tenant_id = ? AND id = ?",
                (tenant, user_id),
            ).fetchone()
        if row is None:
            raise ApiError(404, "user_not_found", "user not found")
        return row

    @api.get("/users/{user_id}")
    def get_user(user_id: str) -> User:
        return _user(find(user_id))

    @api.delete("/users/{user_id}", status_code=204)
    def delete_user(user_id: str) -> Response:
        find(user_id)
        with connect(db) as conn:
            conn.execute(
                "DELETE FROM user_account_users WHERE tenant_id = ? AND id = ?", (tenant, user_id)
            )
        return Response(status_code=204)

    app.include_router(api)
    return app
