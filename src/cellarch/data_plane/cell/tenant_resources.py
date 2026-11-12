"""What a cell creates for each tenant it hosts (docs/plan.md §3.4).

* **Tenant databases:** one database per tenant on the cell's database server, with a
  schema and a login per service. A login can use only its own schema in its own
  tenant's database, and gets a connection limit and a statement timeout
  (noisy-neighbour limits on the shared server). Aurora on AWS; the cell's Postgres
  locally; an SQLite file per tenant as the in-process stand-in.
* **The stack secret:** the tenant's stack credential and each service's database URL,
  written where the tenant's containers read it: a Kubernetes Secret in the cell's
  cluster (Secrets Manager + External Secrets on AWS). It never passes through the
  control plane or the operator's tools.
"""

from __future__ import annotations

import hashlib
import secrets
import ssl
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import psycopg
from psycopg import sql

from cellarch.common.sql import SQLITE_PREFIX, sqlite_url

SERVICES = ("user-account", "order-processing")
STACK_TOKEN_KEY = "stack-token"  # noqa: S105 - a key name, not a secret
SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_PG_NAME_MAX = 63


class TenantResourceError(Exception):
    """A tenant's database or secret could not be created or removed."""


def database_url_key(service: str) -> str:
    return f"{service}-database-url"


def secret_name(tenant_id: str) -> str:
    return f"tenant-{tenant_id}"


def _pg_part(tenant_id: str, budget: int) -> str:
    """The tenant id as a Postgres name part, shortened with a hash to fit ``budget``."""
    name = tenant_id.replace("-", "_")
    if len(name) <= budget:
        return name
    digest = hashlib.sha256(tenant_id.encode()).hexdigest()[:8]
    return f"{name[: budget - 9]}_{digest}"


def schema_name(service: str) -> str:
    return service.replace("-", "_")


def database_name(tenant_id: str) -> str:
    prefix = "tenant_"
    return prefix + _pg_part(tenant_id, _PG_NAME_MAX - len(prefix))


def login_name(tenant_id: str, service: str) -> str:
    suffix = f"_{schema_name(service)}"
    return _pg_part(tenant_id, _PG_NAME_MAX - len(suffix)) + suffix


# --- tenant databases --------------------------------------------------------------------


class TenantDatabases(Protocol):
    def create(self, tenant_id: str) -> dict[str, str]:
        """Create (or repair) the tenant's database; returns each service's DATABASE_URL."""

    def drop(self, tenant_id: str) -> None:
        """Delete the tenant's database and logins. Idempotent."""


class SqliteTenantDatabases:
    """In-process stand-in: one SQLite file per tenant, shared by its services."""

    def __init__(self, directory: str) -> None:
        self._directory = Path(directory)

    def _path(self, tenant_id: str) -> Path:
        return self._directory / f"{tenant_id}.db"

    def create(self, tenant_id: str) -> dict[str, str]:
        self._directory.mkdir(parents=True, exist_ok=True)
        return dict.fromkeys(SERVICES, sqlite_url(self._path(tenant_id)))

    def drop(self, tenant_id: str) -> None:
        database = self._path(tenant_id)
        for path in (database, *(Path(f"{database}{s}") for s in ("-journal", "-wal", "-shm"))):
            path.unlink(missing_ok=True)


class PostgresTenantDatabases:
    """Tenant databases on the cell's Postgres server, managed with the cell's admin login."""

    def __init__(
        self, admin_url: str, *, connection_limit: int = 10, statement_timeout_ms: int = 5000
    ) -> None:
        self._admin_url = admin_url
        self._connection_limit = connection_limit
        self._statement_timeout_ms = statement_timeout_ms

    def _connect(self, database: str | None = None) -> psycopg.Connection[Any]:
        url = self._admin_url
        if database is not None:
            url = urlunsplit(urlsplit(url)._replace(path=f"/{database}"))
        try:
            return psycopg.connect(url, autocommit=True, connect_timeout=5)
        except psycopg.Error as exc:
            raise TenantResourceError("cannot reach the cell's database server") from exc

    def _service_url(self, login: str, password: str, database: str) -> str:
        server = urlsplit(self._admin_url)
        netloc = f"{login}:{quote(password, safe='')}@{server.hostname}:{server.port or 5432}"
        return urlunsplit(("postgresql", netloc, f"/{database}", "", ""))

    def create(self, tenant_id: str) -> dict[str, str]:
        database = database_name(tenant_id)
        db = sql.Identifier(database)
        urls: dict[str, str] = {}
        try:
            with self._connect() as conn:
                if not conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
                ).fetchone():
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(db))
                conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
                for service in SERVICES:
                    login = login_name(tenant_id, service)
                    password = secrets.token_urlsafe(24)
                    exists = conn.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)
                    ).fetchone()
                    # A new password every time: creating again also rotates credentials.
                    conn.execute(
                        sql.SQL("{} ROLE {} WITH LOGIN PASSWORD {} CONNECTION LIMIT {}").format(
                            sql.SQL("ALTER" if exists else "CREATE"),
                            sql.Identifier(login),
                            sql.Literal(password),
                            sql.Literal(self._connection_limit),
                        )
                    )
                    conn.execute(
                        sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                            sql.Identifier(login), sql.Literal(self._statement_timeout_ms)
                        )
                    )
                    conn.execute(
                        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                            db, sql.Identifier(login)
                        )
                    )
                    urls[service] = self._service_url(login, password, database)
            with self._connect(database) as conn:
                conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                for service in SERVICES:
                    role = sql.Identifier(login_name(tenant_id, service))
                    schema = sql.Identifier(schema_name(service))
                    conn.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                            schema, role
                        )
                    )
                    conn.execute(
                        sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path = {}").format(
                            role, db, schema
                        )
                    )
        except psycopg.Error as exc:
            raise TenantResourceError(f"the database server refused: {type(exc).__name__}") from exc
        return urls

    def drop(self, tenant_id: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database_name(tenant_id))
                    )
                )
                for service in SERVICES:
                    conn.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(
                            sql.Identifier(login_name(tenant_id, service))
                        )
                    )
        except psycopg.Error as exc:
            raise TenantResourceError(f"the database server refused: {type(exc).__name__}") from exc


def open_tenant_databases(url: str) -> TenantDatabases:
    """The tenant databases a ``TENANT_DB_ADMIN_URL`` points at."""
    if url.startswith(SQLITE_PREFIX) and len(url) > len(SQLITE_PREFIX):
        return SqliteTenantDatabases(url[len(SQLITE_PREFIX) :])
    if urlsplit(url).scheme in ("postgresql", "postgres"):
        return PostgresTenantDatabases(url)
    raise ValueError(
        "TENANT_DB_ADMIN_URL must be postgresql://<admin>@<host>/postgres or sqlite:///<dir>"
    )


# --- stack secrets -----------------------------------------------------------------------


class TenantSecrets(Protocol):
    def put(self, tenant_id: str, values: dict[str, str]) -> None:
        """Create or replace the tenant's stack secret."""

    def delete(self, tenant_id: str) -> None:
        """Delete the tenant's stack secret. Idempotent."""


class KubernetesSecrets:
    """Stack secrets as Kubernetes Secrets in the cell's namespace.

    The gateway's service account may manage Secrets in its own namespace only (Role in
    charts/cell). Its token rotates, so it is read on every call.
    """

    def __init__(self, namespace: str, *, token_path: str, http: httpx.Client) -> None:
        self._base = f"/api/v1/namespaces/{namespace}/secrets"
        self._token_path = Path(token_path)
        self._http = http

    @classmethod
    def in_cluster(cls, service_account_dir: str = SERVICE_ACCOUNT_DIR) -> KubernetesSecrets:
        directory = Path(service_account_dir)
        try:
            namespace = (directory / "namespace").read_text(encoding="utf-8").strip()
            verify = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        except OSError as exc:
            if isinstance(exc, ssl.SSLError):
                raise TenantResourceError("the service account's CA bundle is unusable") from exc
            raise TenantResourceError(f"no service account in {directory}") from exc
        http = httpx.Client(base_url="https://kubernetes.default.svc", verify=verify, timeout=10)
        return cls(namespace, token_path=str(directory / "token"), http=http)

    def _request(self, method: str, path: str, body: object = None) -> httpx.Response:
        token = self._token_path.read_text(encoding="utf-8").strip()
        try:
            return self._http.request(
                method, path, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise TenantResourceError("cannot reach the Kubernetes API") from exc

    def put(self, tenant_id: str, values: dict[str, str]) -> None:
        name = secret_name(tenant_id)
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/part-of": "cellarch",
                    "cellarch.io/tenant": tenant_id,
                },
            },
            "type": "Opaque",
            "stringData": values,
        }
        response = self._request("POST", self._base, body)
        if response.status_code == 409:  # exists: replace it
            response = self._request("PUT", f"{self._base}/{name}", body)
        if response.status_code >= 300:
            raise TenantResourceError(
                f"the Kubernetes API answered HTTP {response.status_code} for secret {name}"
            )

    def delete(self, tenant_id: str) -> None:
        name = secret_name(tenant_id)
        response = self._request("DELETE", f"{self._base}/{name}")
        if response.status_code >= 300 and response.status_code != 404:
            raise TenantResourceError(
                f"the Kubernetes API answered HTTP {response.status_code} deleting {name}"
            )
