"""The cell's per-tenant resources: databases with a login per service, and the stack secret."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
import pytest

from cellarch.common.sql import DatabaseError, open_database, sqlite_url
from cellarch.data_plane.cell.tenant_resources import (
    SERVICES,
    KubernetesSecrets,
    PostgresTenantDatabases,
    SqliteTenantDatabases,
    TenantResourceError,
    database_name,
    login_name,
    open_tenant_databases,
    secret_name,
)


class TestNames:
    def test_postgres_names_follow_the_tenant(self) -> None:
        assert database_name("acme-co") == "tenant_acme_co"
        assert login_name("acme-co", "user-account") == "acme_co_user_account"
        assert secret_name("acme-co") == "tenant-acme-co"

    def test_long_tenant_ids_stay_within_postgres_limits_and_distinct(self) -> None:
        a, b = "a" * 60 + "-xy", "a" * 60 + "-xz"
        names = {database_name(a), database_name(b), login_name(a, "order-processing")}
        assert all(len(n) <= 63 for n in names)
        assert database_name(a) != database_name(b)


class TestSqliteTenantDatabases:
    def test_one_database_per_tenant_shared_by_its_services(self, tmp_path: Path) -> None:
        databases = SqliteTenantDatabases(str(tmp_path / "tenants"))
        urls = databases.create("acme")
        assert set(urls) == set(SERVICES)
        assert len(set(urls.values())) == 1
        assert urls == databases.create("acme")  # idempotent
        assert urls != databases.create("globex")

    def test_drop_deletes_the_database(self, tmp_path: Path) -> None:
        databases = SqliteTenantDatabases(str(tmp_path / "tenants"))
        url = databases.create("acme")["user-account"]
        with open_database(url).connect() as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
        databases.drop("acme")
        databases.drop("acme")  # idempotent
        assert not list((tmp_path / "tenants").iterdir())


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("sqlite:///./data/tenants", SqliteTenantDatabases),
        ("postgresql://postgres:pw@db:5432/postgres", PostgresTenantDatabases),
    ],
)
def test_open_tenant_databases(url: str, kind: type) -> None:
    assert isinstance(open_tenant_databases(url), kind)


def test_open_tenant_databases_rejects_other_urls_without_echoing_them() -> None:
    with pytest.raises(ValueError, match="TENANT_DB_ADMIN_URL") as raised:
        open_tenant_databases("mysql://root:secret@db/x")
    assert "secret" not in str(raised.value)


def test_unreachable_postgres_server_raises_a_resource_error() -> None:
    databases = PostgresTenantDatabases("postgresql://postgres:pw@127.0.0.1:1/postgres")
    with pytest.raises(TenantResourceError, match="database server"):
        databases.create("acme")


# --- Kubernetes secrets ------------------------------------------------------------------


class FakeKubernetesApi:
    """The slice of the Kubernetes API the secret store uses, as an httpx transport."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, object]] = {}
        self.requests: list[tuple[str, str, str]] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        self.requests.append((request.method, request.url.path, auth))
        if self.fail:
            return httpx.Response(500, json={"message": "boom"})
        prefix = "/api/v1/namespaces/cell/secrets"
        if request.method == "POST" and request.url.path == prefix:
            body = json.loads(request.content)
            name = body["metadata"]["name"]
            if name in self.secrets:
                return httpx.Response(409, json={"reason": "AlreadyExists"})
            self.secrets[name] = body
            return httpx.Response(201, json=body)
        name = request.url.path.removeprefix(prefix + "/")
        if request.method == "PUT":
            if name not in self.secrets:
                return httpx.Response(404, json={})
            self.secrets[name] = json.loads(request.content)
            return httpx.Response(200, json=self.secrets[name])
        if request.method == "DELETE":
            if self.secrets.pop(name, None) is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={})
        return httpx.Response(405, json={})


@pytest.fixture
def kube(tmp_path: Path) -> tuple[FakeKubernetesApi, KubernetesSecrets]:
    api = FakeKubernetesApi()
    token = tmp_path / "token"
    token.write_text("sa-token-1", encoding="utf-8")
    store = KubernetesSecrets(
        "cell",
        token_path=str(token),
        http=httpx.Client(
            base_url="https://kubernetes.default.svc", transport=httpx.MockTransport(api.handler)
        ),
    )
    return api, store


class TestKubernetesSecrets:
    def test_creates_then_replaces_the_tenants_secret(
        self, kube: tuple[FakeKubernetesApi, KubernetesSecrets]
    ) -> None:
        api, store = kube
        store.put("acme", {"stack-token": "one"})
        store.put("acme", {"stack-token": "two"})
        secret = api.secrets["tenant-acme"]
        assert secret["stringData"] == {"stack-token": "two"}
        assert secret["metadata"]["labels"]["cellarch.io/tenant"] == "acme"  # type: ignore[index]
        assert [r[0] for r in api.requests] == ["POST", "POST", "PUT"]
        assert all(r[2] == "Bearer sa-token-1" for r in api.requests)

    def test_reads_the_rotating_token_on_every_call(
        self, kube: tuple[FakeKubernetesApi, KubernetesSecrets], tmp_path: Path
    ) -> None:
        api, store = kube
        store.put("acme", {"k": "v"})
        (tmp_path / "token").write_text("sa-token-2", encoding="utf-8")
        store.delete("acme")
        assert api.requests[-1] == (
            "DELETE",
            "/api/v1/namespaces/cell/secrets/tenant-acme",
            "Bearer sa-token-2",
        )

    def test_delete_is_idempotent(self, kube: tuple[FakeKubernetesApi, KubernetesSecrets]) -> None:
        api, store = kube
        store.delete("nobody")
        assert api.secrets == {}

    def test_api_errors_raise_without_secret_values(
        self, kube: tuple[FakeKubernetesApi, KubernetesSecrets]
    ) -> None:
        api, store = kube
        api.fail = True
        with pytest.raises(TenantResourceError) as raised:
            store.put("acme", {"stack-token": "do-not-leak"})
        assert "do-not-leak" not in str(raised.value)
        with pytest.raises(TenantResourceError):
            store.delete("acme")

    def test_unreachable_api_raises(self, tmp_path: Path) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        (tmp_path / "token").write_text("t", encoding="utf-8")
        store = KubernetesSecrets(
            "cell",
            token_path=str(tmp_path / "token"),
            http=httpx.Client(base_url="https://k8s", transport=httpx.MockTransport(refuse)),
        )
        with pytest.raises(TenantResourceError, match="Kubernetes API"):
            store.put("acme", {})

    def test_in_cluster_configuration(self, tmp_path: Path) -> None:
        for name, value in {"token": "t", "namespace": "cell-ns", "ca.crt": ""}.items():
            (tmp_path / name).write_text(value, encoding="utf-8")
        with pytest.raises(TenantResourceError):  # an empty CA bundle is not usable
            KubernetesSecrets.in_cluster(str(tmp_path))
        with pytest.raises(TenantResourceError, match="service account"):
            KubernetesSecrets.in_cluster(str(tmp_path / "missing"))


# --- PostgreSQL (adapter tests, A56 in tests/acceptance) ---------------------------------


class TestPostgresTenantDatabases:
    def test_creates_a_database_and_a_login_per_service(self, postgres_url: str) -> None:
        databases = PostgresTenantDatabases(postgres_url)
        urls = databases.create("acme")
        try:
            assert set(urls) == set(SERVICES)
            for service, url in urls.items():
                assert f"{login_name('acme', service)}:" in url
                assert url.endswith(f"/{database_name('acme')}")
                open_database(url).ping()
        finally:
            databases.drop("acme")

    def test_create_again_rotates_the_passwords(self, postgres_url: str) -> None:
        databases = PostgresTenantDatabases(postgres_url)
        first = databases.create("acme")
        try:
            second = databases.create("acme")
            assert first != second
            open_database(second["user-account"]).ping()
            with pytest.raises(DatabaseError):
                open_database(first["user-account"]).ping()
        finally:
            databases.drop("acme")

    def test_drop_removes_database_and_logins(self, postgres_url: str) -> None:
        databases = PostgresTenantDatabases(postgres_url)
        url = databases.create("acme")["user-account"]
        databases.drop("acme")
        databases.drop("acme")  # idempotent
        with pytest.raises(DatabaseError):
            open_database(url).ping()
        with psycopg.connect(postgres_url) as conn:
            roles = conn.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE 'acme_%'"
            ).fetchall()
        assert roles == []


def test_sqlite_url_round_trip(tmp_path: Path) -> None:
    databases = SqliteTenantDatabases(str(tmp_path))
    assert databases.create("acme")["order-processing"] == sqlite_url(tmp_path / "acme.db")


def test_in_cluster_with_a_valid_ca_bundle(tmp_path: Path) -> None:
    import shutil

    import certifi

    (tmp_path / "token").write_text("t", encoding="utf-8")
    (tmp_path / "namespace").write_text("cell-ns\n", encoding="utf-8")
    shutil.copy(certifi.where(), tmp_path / "ca.crt")
    store = KubernetesSecrets.in_cluster(str(tmp_path))
    assert store._base == "/api/v1/namespaces/cell-ns/secrets"


def test_a_server_that_refuses_is_reported(postgres_url: str) -> None:
    """An admin login without the needed privileges: create and drop are refused."""
    weak = "weak_admin_for_tests"
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute(f"DROP ROLE IF EXISTS {weak}")
        conn.execute(f"CREATE ROLE {weak} LOGIN PASSWORD 'weak-pw'")
    parts = urlsplit(postgres_url)
    weak_url = urlunsplit(parts._replace(netloc=f"{weak}:weak-pw@{parts.hostname}:{parts.port}"))
    strong = PostgresTenantDatabases(postgres_url)
    strong.create("acme")
    try:
        with pytest.raises(TenantResourceError, match="refused"):
            PostgresTenantDatabases(weak_url).create("globex")
        with pytest.raises(TenantResourceError, match="refused"):
            PostgresTenantDatabases(weak_url).drop("acme")
    finally:
        strong.drop("acme")
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            conn.execute(f"DROP ROLE IF EXISTS {weak}")
