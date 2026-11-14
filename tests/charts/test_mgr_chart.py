"""charts/mgr: the web UI's GraphQL backend, one release in every cell's cluster (§2.4)."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import RenderError, containers, env, find, lint, render

REQUIRED: dict[str, Any] = {"cellId": "cell-1", "image": {"tag": "0.1.0-abc1234"}}


def test_lints_cleanly() -> None:
    result = lint("mgr", REQUIRED)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("values", "missing"),
    [({"image": {"tag": "1.0.0"}}, "cellId"), ({"cellId": "cell-1"}, "image.tag")],
)
def test_required_values(values: dict[str, Any], missing: str) -> None:
    with pytest.raises(RenderError, match=missing):
        render("mgr", values)


def test_runs_its_own_versioned_image_next_to_the_gateway() -> None:
    objects = render("mgr", REQUIRED)
    (container,) = containers(objects)
    variables = env(container)
    assert container["image"] == "cellarch/mgr:0.1.0-abc1234"  # its own version, never :dev
    assert variables["CELL_ID"] == "cell-1"
    assert variables["GATEWAY_URL"] == "http://cellarch-cell-gateway:8000"
    assert variables["MGR_TOKEN"] == {"secretKeyRef": {"name": "cellarch-cell", "key": "mgr-token"}}
    assert variables["GRAPHQL_INTROSPECTION"] == "false"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["resources"]["limits"]["memory"]
    pod = find(objects, "Deployment", "mgr")["spec"]["template"]["spec"]
    assert pod["priorityClassName"] == "cellarch-ui"  # below the gateway and tenant services
    assert pod["automountServiceAccountToken"] is False


def test_the_gateway_finds_it_by_a_fixed_name() -> None:
    service = find(render("mgr", REQUIRED), "Service", "mgr")
    assert service["metadata"]["name"] == "mgr"
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 8000


def test_scales_on_its_own() -> None:
    objects = render("mgr", {**REQUIRED, "autoscaling": {"minReplicas": 2, "maxReplicas": 9}})
    hpa = find(objects, "HorizontalPodAutoscaler", "mgr")
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (2, 9)
    assert hpa["spec"]["scaleTargetRef"]["name"] == "mgr"
    pdb = find(objects, "PodDisruptionBudget", "mgr")
    assert pdb["spec"]["maxUnavailable"] == 1
    off = render("mgr", {**REQUIRED, "autoscaling": {"enabled": False}, "replicas": 3})
    assert not [o for o in off if o["kind"] == "HorizontalPodAutoscaler"]
    assert find(off, "Deployment", "mgr")["spec"]["replicas"] == 3


def test_only_its_cells_gateway_may_call_it() -> None:
    policy = find(render("mgr", REQUIRED), "NetworkPolicy", "mgr")
    (rule,) = policy["spec"]["ingress"]
    assert rule["from"] == [
        {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/component": "gateway",
                    "app.kubernetes.io/part-of": "cellarch",
                }
            }
        }
    ]
    assert rule["ports"] == [{"port": 8000, "protocol": "TCP"}]
