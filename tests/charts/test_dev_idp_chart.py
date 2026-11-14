"""charts/dev-idp: the development-only identity provider in the local platform cluster."""

from __future__ import annotations

from .conftest import containers, env, find, lint, render


def test_lints_cleanly() -> None:
    result = lint("dev-idp", {})
    assert result.returncode == 0, result.stdout + result.stderr


def test_issues_sessions_for_tenant_hosts() -> None:
    objects = render("dev-idp", {"baseDomain": "localhost", "issuer": "http://localhost:8080/auth"})
    (container,) = containers(objects)
    variables = env(container)
    assert container["image"] == "cellarch/dev-idp:dev"
    assert variables["TENANT_BASE_DOMAIN"] == "localhost"
    assert variables["DEV_IDP_ISSUER"] == "http://localhost:8080/auth"
    assert variables["SESSION_AUDIENCE"] == "cellarch-web"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["readinessProbe"]["httpGet"]["path"] == "/auth/healthz"
    service = find(objects, "Service", "dev-idp")
    assert (service["metadata"]["name"], service["spec"]["type"]) == ("dev-idp", "ClusterIP")


def test_reachable_from_the_edge_and_the_router_only() -> None:
    policy = find(render("dev-idp", {}), "NetworkPolicy", "dev-idp")
    (rule,) = policy["spec"]["ingress"]
    components = sorted(
        p["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for p in rule["from"]
    )
    assert components == ["edge", "router"]
