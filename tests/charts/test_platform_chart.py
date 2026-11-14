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
    edge = images.pop("edge")  # the local edge proxy: a pinned nginx image, not ours
    assert images == {
        "router": "reg.example/cellarch/router:dev",
        "control-plane": "reg.example/cellarch/control-plane:dev",
    }
    assert "@sha256:" in edge


def test_containers_are_hardened_with_probes_and_limits() -> None:
    objects = render("platform", {**REQUIRED, "redis": {"enabled": True}})
    assert len(containers(objects)) == 4  # router, control plane, Redis, edge
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
    assert "DEFAULT_TENANT_ISOLATION" not in variables
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
    objects = render("platform", REQUIRED)
    assert find(objects, "Service", "router")["spec"]["type"] == "ClusterIP"
    assert find(objects, "Service", "control-plane")["spec"]["type"] == "ClusterIP"
    alone = render(
        "platform",
        {**REQUIRED, "edge": {"enabled": False}, "router": {"service": {"type": "LoadBalancer"}}},
    )
    assert find(alone, "Service", "router")["spec"]["type"] == "LoadBalancer"
    assert not [o for o in alone if "edge" in o["metadata"]["name"]]


def test_the_edge_serves_every_tenant_host_on_one_port() -> None:
    """Local stand-in for the production ALB's path rules (docs/plan.md §2.4)."""
    objects = render("platform", {**REQUIRED, "edge": {"service": {"port": 8080}}})
    service = find(objects, "Service", "edge")
    assert (service["spec"]["type"], service["spec"]["ports"][0]["port"]) == ("LoadBalancer", 8080)
    edge = find(objects, "Deployment", "edge")
    (container,) = edge["spec"]["template"]["spec"]["containers"]
    assert "nginx-unprivileged" in container["image"] and "@sha256:" in container["image"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    config = find(objects, "ConfigMap", "edge")["data"]["default.conf"]
    assert "location /auth/" in config and "http://dev-idp:8000" in config
    assert "location /" in config and "-router:8000" in config
    assert "proxy_set_header Host $host" in config  # the tenant comes from the host


def test_with_the_web_ui_the_edge_sends_pages_to_it_and_api_paths_to_the_router() -> None:
    objects = render("platform", {**REQUIRED, "edge": {"webUiUrl": "http://web-ui:8080"}})
    config = find(objects, "ConfigMap", "edge")["data"]["default.conf"]
    blocks = {
        line.strip().split()[1]: next_line.strip()
        for line, next_line in zip(config.splitlines(), config.splitlines()[1:], strict=False)
        if line.strip().startswith("location ")
    }
    assert "http://web-ui:8080" in blocks["/"]
    for api in ("/graphql", "/v1/", "/_router/"):
        assert "-router:8000" in blocks[api], api
    assert "http://dev-idp:8000" in blocks["/auth/"]


def test_the_router_checks_web_ui_sessions() -> None:
    objects = render(
        "platform",
        {
            **REQUIRED,
            "sessions": {"issuer": "http://localhost:8080/auth", "baseDomain": "localhost"},
        },
    )
    router = next(c for c in containers(objects) if c["name"] == "router")
    variables = env(router)
    assert variables["SESSION_JWKS_URL"] == "http://dev-idp:8000/auth/.well-known/jwks.json"
    assert variables["SESSION_ISSUER"] == "http://localhost:8080/auth"
    assert variables["SESSION_AUDIENCE"] == "cellarch-web"
    assert variables["TENANT_BASE_DOMAIN"] == "localhost"


def test_network_policies_default_deny_and_explicit_allows() -> None:
    objects = render("platform", {**REQUIRED, "redis": {"enabled": True}})
    policies = {o["metadata"]["name"]: o for o in objects if o["kind"] == "NetworkPolicy"}
    deny = next(p for name, p in policies.items() if name.endswith("default-deny"))
    assert deny["spec"]["podSelector"] == {} and deny["spec"]["policyTypes"] == ["Ingress"]
    redis_policy = next(p for name, p in policies.items() if name.endswith("redis"))
    allowed = redis_policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    assert allowed["app.kubernetes.io/component"] == "router"
