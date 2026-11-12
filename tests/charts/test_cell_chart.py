"""charts/cell: one cell's gateway, installed into that cell's own cluster."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import RenderError, containers, env, find, lint, render

REQUIRED: dict[str, Any] = {"cellId": "cell-1", "secrets": {"internalToken": "cell-secret-value"}}


def test_lints_cleanly() -> None:
    result = lint("cell", REQUIRED)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("values", "missing"),
    [({"secrets": {"internalToken": "x"}}, "cellId"), ({"cellId": "cell-1"}, "internalToken")],
)
def test_required_values(values: dict[str, Any], missing: str) -> None:
    with pytest.raises(RenderError, match=missing):
        render("cell", values)


def test_gateway_image_settings_and_hardening() -> None:
    objects = render("cell", {**REQUIRED, "gateway": {"maxConcurrentRequests": 50}})
    (container,) = containers(objects)
    variables = env(container)
    assert container["image"] == "cellarch/cell-gateway:dev"
    assert variables["CELL_ID"] == "cell-1"
    assert variables["MAX_CONCURRENT_REQUESTS"] == "50"
    assert variables["CELL_DB_PATH"] == "/data/cell-1.db"
    assert "cell-secret-value" not in str(variables)
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"


def test_gateway_is_reachable_on_the_cells_load_balancer() -> None:
    objects = render("cell", REQUIRED)
    service = find(objects, "Service", "gateway")
    assert service["spec"]["type"] == "LoadBalancer"
    assert service["spec"]["ports"][0]["port"] == 8000
    assert find(objects, "PersistentVolumeClaim", "gateway")
    assert find(objects, "Deployment", "gateway")["spec"]["strategy"]["type"] == "Recreate"


def test_labels_identify_the_cell() -> None:
    objects = render("cell", REQUIRED)
    labels = find(objects, "Deployment", "gateway")["metadata"]["labels"]
    assert labels["cellarch.io/cell"] == "cell-1"
    assert labels["app.kubernetes.io/part-of"] == "cellarch"


def test_network_policies() -> None:
    objects = render("cell", REQUIRED)
    policies = [o for o in objects if o["kind"] == "NetworkPolicy"]
    names = sorted(p["metadata"]["name"] for p in policies)
    assert any(n.endswith("default-deny") for n in names)
    gateway_policy = next(p for p in policies if p["metadata"]["name"].endswith("gateway"))
    assert gateway_policy["spec"]["ingress"][0]["ports"] == [{"port": 8000, "protocol": "TCP"}]


def test_the_cell_has_its_own_database_server() -> None:
    objects = render("cell", REQUIRED)
    server = find(objects, "StatefulSet", "postgres")
    (container,) = server["spec"]["template"]["spec"]["containers"]
    assert container["image"].startswith("postgres:17")
    assert "@sha256:" in container["image"]  # pinned
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["readinessProbe"]["exec"]["command"][0] == "pg_isready"
    assert server["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"]
    service = find(objects, "Service", "postgres")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 5432


def test_the_gateway_manages_tenant_databases_and_secrets() -> None:
    objects = render("cell", REQUIRED)
    deployment = find(objects, "Deployment", "gateway")
    pod = deployment["spec"]["template"]["spec"]
    variables = env(pod["containers"][0])
    assert pod["automountServiceAccountToken"] is True  # to write tenant stack secrets
    assert pod["serviceAccountName"].endswith("gateway")
    assert variables["TENANT_DB_ADMIN_URL"].startswith(
        "postgresql://postgres:$(POSTGRES_PASSWORD)@"
    )
    assert variables["TENANT_DB_ADMIN_URL"].endswith("-postgres:5432/postgres")
    assert variables["TENANT_SERVICE_URL"] == "http://tenant-{tenant}-{service}:8000"
    assert variables["POSTGRES_PASSWORD"]["secretKeyRef"]["key"] == "postgres-password"
    role = find(objects, "Role", "gateway")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "update", "delete"]}
    ]  # never get or list: it cannot read other secrets
    binding = find(objects, "RoleBinding", "gateway")
    assert binding["subjects"][0]["name"] == pod["serviceAccountName"]


def test_the_database_password_is_generated_in_the_cluster() -> None:
    objects = render("cell", REQUIRED)
    secret = find(objects, "Secret", "cell")
    password = secret["stringData"]["postgres-password"]
    assert len(password) == 32 and password.isalnum()


def test_only_the_gateway_and_tenant_services_reach_the_database() -> None:
    objects = render("cell", REQUIRED)
    policy = find(objects, "NetworkPolicy", "postgres")
    peers = policy["spec"]["ingress"][0]["from"]
    selectors = [p["podSelector"]["matchLabels"] for p in peers]
    assert {
        "app.kubernetes.io/component": "gateway",
        "app.kubernetes.io/part-of": "cellarch",
    } in selectors
    assert {"cellarch.io/role": "tenant-service"} in selectors
    assert policy["spec"]["ingress"][0]["ports"] == [{"port": 5432, "protocol": "TCP"}]
