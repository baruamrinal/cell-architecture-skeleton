"""charts/web-ui: the tenant web UI's static files, a platform service (docs/plan.md §2.4)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import RenderError, containers, find, lint, render

REQUIRED: dict[str, Any] = {"image": {"tag": "0.1.0-abc1234"}}


def test_lints_cleanly() -> None:
    result = lint("web-ui", REQUIRED)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_image_tag_is_required() -> None:
    with pytest.raises(RenderError, match="image.tag"):
        render("web-ui", {})


def test_serves_its_own_versioned_image_hardened() -> None:
    objects = render("web-ui", REQUIRED)
    (container,) = containers(objects)
    assert container["image"] == "cellarch/web-ui:0.1.0-abc1234"
    security = container["securityContext"]
    assert security["readOnlyRootFilesystem"] is True and security["runAsNonRoot"] is True
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["resources"]["limits"]["memory"]
    service = find(objects, "Service", "web-ui")
    assert (service["metadata"]["name"], service["spec"]["ports"][0]["port"]) == ("web-ui", 8080)


def test_runtime_configuration_comes_from_the_chart() -> None:
    objects = render("web-ui", {**REQUIRED, "config": {"signIn": {"provider": "oidc"}}})
    config = json.loads(find(objects, "ConfigMap", "web-ui")["data"]["config.json"])
    assert config["graphqlPath"] == "/graphql"
    assert config["signIn"]["provider"] == "oidc"
    (container,) = containers(objects)
    mount = next(m for m in container["volumeMounts"] if m["name"] == "config")
    assert mount == {
        "name": "config",
        "mountPath": "/usr/share/nginx/html/config.json",
        "subPath": "config.json",
        "readOnly": True,
    }


def test_scales_on_its_own() -> None:
    objects = render("web-ui", {**REQUIRED, "autoscaling": {"minReplicas": 2, "maxReplicas": 6}})
    hpa = find(objects, "HorizontalPodAutoscaler", "web-ui")
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (2, 6)
    assert find(objects, "PodDisruptionBudget", "web-ui")["spec"]["maxUnavailable"] == 1


def test_reachable_from_the_edge_only_and_calls_nothing() -> None:
    policy = find(render("web-ui", REQUIRED), "NetworkPolicy", "web-ui")
    assert sorted(policy["spec"]["policyTypes"]) == ["Egress", "Ingress"]
    assert policy["spec"].get("egress", []) == []  # static files: no outbound calls
    (rule,) = policy["spec"]["ingress"]
    labels = rule["from"][0]["podSelector"]["matchLabels"]
    assert labels["app.kubernetes.io/component"] == "edge"
