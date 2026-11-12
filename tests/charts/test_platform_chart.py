"""charts/platform: router + control plane (+ optional Redis), in the platform cluster."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import RenderError, containers, env, find, lint, render

pytestmark = pytest.mark.usefixtures()

REQUIRED: dict[str, Any] = {
    "secrets": {
        "adminToken": "admin-secret-value",
        "routerToken": "router-secret-value",
        "metricsToken": "metrics-secret-value",
        "cellCredentials": {"cell-1": "cell-1-secret-value"},
    },
    "controlPlane": {"seedCells": {"cell-1": "http://cell-1.cellarch.internal:8000"}},
}


def test_lints_cleanly() -> None:
    result = lint("platform", REQUIRED)
    assert result.returncode == 0, result.stdout + result.stderr


def test_required_secrets() -> None:
    with pytest.raises(RenderError, match="adminToken"):
        render("platform", {"secrets": {"routerToken": "x", "metricsToken": "y"}})


def test_images_one_per_component() -> None:
    objects = render("platform", {**REQUIRED, "image": {"repository": "reg.example/cellarch"}})
    images = {c["name"]: c["image"] for c in containers(objects)}
    assert images == {
        "router": "reg.example/cellarch/router:dev",
        "control-plane": "reg.example/cellarch/control-plane:dev",
    }


def test_containers_are_hardened_with_probes_and_limits() -> None:
    objects = render("platform", {**REQUIRED, "redis": {"enabled": True}})
    assert len(containers(objects)) == 3
    for container in containers(objects):
        security = container["securityContext"]
        assert security["runAsNonRoot"] is True, container["name"]
        assert security["readOnlyRootFilesystem"] is True, container["name"]
        assert security["allowPrivilegeEscalation"] is False, container["name"]
        assert security["capabilities"]["drop"] == ["ALL"], container["name"]
        assert container["resources"]["limits"]["memory"], container["name"]
        assert container["livenessProbe"] and container["readinessProbe"], container["name"]


def test_tokens_come_from_a_secret_never_plain_env() -> None:
    objects = render("platform", REQUIRED)
    secret = find(objects, "Secret", "platform")
    assert set(secret["stringData"]) >= {
        "admin-token",
        "router-token",
        "metrics-token",
        "cell-credentials",
    }
    assert secret["stringData"]["cell-credentials"] == "cell-1=cell-1-secret-value"
    rendered_env = str([env(c) for c in containers(objects)])
    assert "secret-value" not in rendered_env  # only secretKeyRef references


def test_router_settings() -> None:
    objects = render("platform", {**REQUIRED, "router": {"replicas": 3}})
    router = find(objects, "Deployment", "router")
    variables = env(router["spec"]["template"]["spec"]["containers"][0])
    assert router["spec"]["replicas"] == 3
    assert variables["ROUTER_REPLICAS"] == "3"  # each replica enforces limit / 3
    assert variables["CONTROL_PLANE_URL"] == "http://test-platform-control-plane:8000"
    assert variables["RATE_LIMIT_BACKEND"] == "memory"


def test_control_plane_settings_and_storage() -> None:
    objects = render("platform", REQUIRED)
    deployment = find(objects, "Deployment", "control-plane")
    variables = env(deployment["spec"]["template"]["spec"]["containers"][0])
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"  # single-writer SQLite volume
    assert variables["SEED_CELLS"] == "cell-1=http://cell-1.cellarch.internal:8000"
    assert variables["CONTROL_PLANE_DB_PATH"] == "/data/control-plane.db"
    assert find(objects, "PersistentVolumeClaim", "control-plane")


def test_redis_is_opt_in() -> None:
    assert not [o for o in render("platform", REQUIRED) if "redis" in o["metadata"]["name"]]
    objects = render("platform", {**REQUIRED, "redis": {"enabled": True}})
    router_env = env(
        find(objects, "Deployment", "router")["spec"]["template"]["spec"]["containers"][0]
    )
    assert router_env["RATE_LIMIT_BACKEND"] == "redis"
    assert router_env["REDIS_URL"] == "redis://test-platform-redis:6379/0"
    assert find(objects, "Service", "redis")


def test_services_and_exposure() -> None:
    objects = render(
        "platform",
        {**REQUIRED, "router": {"service": {"type": "LoadBalancer", "port": 8080}}},
    )
    router_service = find(objects, "Service", "router")
    assert router_service["spec"]["type"] == "LoadBalancer"
    assert router_service["spec"]["ports"][0]["port"] == 8080
    assert find(objects, "Service", "control-plane")["spec"]["type"] == "ClusterIP"


def test_network_policies_default_deny_and_explicit_allows() -> None:
    objects = render("platform", {**REQUIRED, "redis": {"enabled": True}})
    policies = {o["metadata"]["name"]: o for o in objects if o["kind"] == "NetworkPolicy"}
    deny = next(p for name, p in policies.items() if name.endswith("default-deny"))
    assert deny["spec"]["podSelector"] == {} and deny["spec"]["policyTypes"] == ["Ingress"]
    redis_policy = next(p for name, p in policies.items() if name.endswith("redis"))
    allowed = redis_policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    assert allowed["app.kubernetes.io/component"] == "router"
