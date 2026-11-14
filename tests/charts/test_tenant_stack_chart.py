"""charts/tenant-stack: one tenant's services, installed into its cell's cluster."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import RenderError, env, find, lint, render

REQUIRED: dict[str, Any] = {"tenantId": "acme"}


def test_lints_cleanly() -> None:
    result = lint("tenant-stack", REQUIRED)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("values", [{}, {"tenantId": "Not_Valid"}])
def test_tenant_id_is_required_and_validated(values: dict[str, Any]) -> None:
    with pytest.raises(RenderError, match="tenantId"):
        render("tenant-stack", values)


def test_two_services_named_where_the_gateway_looks_for_them() -> None:
    objects = render("tenant-stack", REQUIRED)
    services = sorted(o["metadata"]["name"] for o in objects if o["kind"] == "Service")
    assert services == ["tenant-acme-order-processing", "tenant-acme-user-account"]
    for name in services:
        assert find(objects, "Service", name)["spec"]["ports"][0]["port"] == 8000


@pytest.mark.parametrize("service", ["user-account", "order-processing"])
def test_each_service_reads_its_settings_from_the_tenants_secret(service: str) -> None:
    objects = render("tenant-stack", REQUIRED)
    deployment = find(objects, "Deployment", f"tenant-acme-{service}")
    pod = deployment["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    variables = env(container)
    assert container["image"] == f"cellarch/{service}:dev"
    assert variables["TENANT_ID"] == "acme"
    assert variables["STACK_TOKEN"]["secretKeyRef"] == {"name": "tenant-acme", "key": "stack-token"}
    assert variables["DATABASE_URL"]["secretKeyRef"] == {
        "name": "tenant-acme",
        "key": f"{service}-database-url",
    }
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["automountServiceAccountToken"] is False
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert labels["cellarch.io/tenant"] == "acme"
    assert labels["cellarch.io/role"] == "tenant-service"


def test_order_processing_calls_its_own_tenants_user_account() -> None:
    objects = render("tenant-stack", REQUIRED)
    container = find(objects, "Deployment", "order-processing")["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert env(container)["USER_ACCOUNT_URL"] == "http://tenant-acme-user-account:8000"


@pytest.mark.parametrize(
    ("tier", "cpu", "memory"), [("standard", "250m", "256Mi"), ("premium", "1", "512Mi")]
)
def test_resource_caps_follow_the_tier(tier: str, cpu: str, memory: str) -> None:
    objects = render("tenant-stack", {**REQUIRED, "tier": tier})
    for o in objects:
        if o["kind"] == "Deployment":
            limits = o["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
            assert (str(limits["cpu"]), limits["memory"]) == (cpu, memory)


def test_network_policies_allow_only_the_gateway_and_the_sibling() -> None:
    objects = render("tenant-stack", REQUIRED)
    gateway = {"app.kubernetes.io/component": "gateway", "app.kubernetes.io/part-of": "cellarch"}
    users = find(objects, "NetworkPolicy", "user-account")
    orders = find(objects, "NetworkPolicy", "order-processing")
    users_from = [p["podSelector"]["matchLabels"] for p in users["spec"]["ingress"][0]["from"]]
    orders_from = [p["podSelector"]["matchLabels"] for p in orders["spec"]["ingress"][0]["from"]]
    assert gateway in users_from
    assert {
        "cellarch.io/tenant": "acme",
        "app.kubernetes.io/component": "order-processing",
    } in users_from
    assert orders_from == [gateway]


def test_tenant_services_are_scheduled_as_api() -> None:
    from .conftest import find

    objects = render("tenant-stack", REQUIRED)
    for name in ("user-account", "order-processing"):
        pod = find(objects, "Deployment", name)["spec"]["template"]["spec"]
        assert pod["priorityClassName"] == "cellarch-api"
