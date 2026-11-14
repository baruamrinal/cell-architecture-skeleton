from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cellarch.cli.cellctl import main
from cellarch.cli.clusters import ClusterError
from cellarch.control_plane.app import ControlPlaneSettings, create_app

from ..control_plane.test_cell_lifecycle_unit import fake_cells

ADMIN = "admin-t"
CELL_3_TOKEN = "cell-3-credential-0123"


@pytest.fixture
def control_plane(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        ControlPlaneSettings(
            db_path=str(tmp_path / "cp.db"),
            admin_token=ADMIN,
            router_token="router-t",
            cell_credentials={"cell-1": "c1"},
        ),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(fake_cells({"cell-1": "c1", "cell-3": CELL_3_TOKEN}))
        ),
    )
    with TestClient(app, base_url="http://cp") as client:
        yield client


class FakeClusters:
    def __init__(self, existing: dict[str, str] | None = None, fail_on: str | None = None):
        self.existing = dict(existing or {})
        self.fail_on = fail_on
        self.calls: list[tuple[Any, ...]] = []

    def _record(self, *call: Any) -> None:
        self.calls.append(call)
        if self.fail_on == call[0]:
            raise ClusterError(f"{call[0]} failed")

    def clusters(self) -> dict[str, str]:
        return dict(self.existing)

    def cell_ids(self) -> list[str]:
        return sorted(c for c in self.existing if c != "platform")

    def ensure_cluster(self, short: str, *, ports: Iterable[str] = ()) -> None:
        self._record("cluster", short)
        self.existing[short] = "running"

    def import_images(self, short: str, images: Sequence[str]) -> None:
        self._record("import", short, tuple(images))

    def helm_upgrade(
        self, short: str, release: str, chart: Path, values: Mapping[str, Any], **kw: Any
    ) -> None:
        self._record("helm", short, chart.name, dict(values))

    def helm_uninstall(self, short: str, release: str) -> None:
        self._record("uninstall", short, release)

    def ensure_network(self) -> None:
        pass

    def delete_network(self) -> None:
        pass

    def node_ip(self, short: str) -> str:
        return "172.20.0.9"

    def set_cell_dns(self, entries: Mapping[str, str]) -> bool:
        self._record("dns", tuple(sorted(entries)))
        return True

    def stop(self, short: str) -> None:
        self._record("stop", short)
        self.existing[short] = "stopped"

    def delete(self, short: str) -> None:
        self._record("delete", short)
        self.existing.pop(short, None)

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def charts(tmp_path: Path) -> Path:
    (tmp_path / "charts" / "cell").mkdir(parents=True)
    return tmp_path / "charts"


def run(client: TestClient, *args: str, clusters: Any = None, charts: Path | None = None) -> int:
    prefix = ["--charts-dir", str(charts)] if charts else []
    return main([*prefix, *args], client=client, token=ADMIN, clusters=clusters)


class TestRegisterWithCredential:
    def test_register_passes_the_credential_and_never_prints_it(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run(
            control_plane,
            "cells",
            "register",
            "cell-3",
            "http://cell-3",
            "--credential",
            CELL_3_TOKEN,
        )
        assert code == 0
        out = capsys.readouterr().out
        assert json.loads(out)["cell_id"] == "cell-3"
        assert CELL_3_TOKEN not in out


class TestSetCapacity:
    def test_set_capacity(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        capsys.readouterr()
        assert run(control_plane, "cells", "set-capacity", "cell-1", "12") == 0
        assert json.loads(capsys.readouterr().out)["capacity"] == 12

    def test_capacity_must_be_positive(self, control_plane: TestClient) -> None:
        with pytest.raises(SystemExit) as exc:
            run(control_plane, "cells", "set-capacity", "cell-1", "0")
        assert exc.value.code == 2


class TestStatus:
    def test_status_shows_totals_cells_and_warnings(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1", "--capacity", "1")
        capsys.readouterr()
        assert run(control_plane, "status") == 0
        out = capsys.readouterr().out
        assert "Cells: 1 (1 placeable)" in out
        assert "Free slots: 1" in out
        assert "Target utilization: 75%" in out
        assert "capacity_low" in out

    def test_status_without_warnings_and_without_cells(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(control_plane, "status") == 0
        assert "no_capacity" in capsys.readouterr().out
        run(control_plane, "cells", "register", "cell-1", "http://cell-1", "--capacity", "10")
        capsys.readouterr()
        run(control_plane, "status")
        assert "No warnings." in capsys.readouterr().out


class TestDeploy:
    def test_refuses_an_existing_cell_without_touching_clusters(
        self, control_plane: TestClient, charts: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        clusters = FakeClusters()
        assert (
            run(control_plane, "cells", "deploy", "cell-1", clusters=clusters, charts=charts) == 1
        )
        assert clusters.calls == []
        assert "cell_exists" in capsys.readouterr().err

    def test_refuses_when_the_cluster_already_exists(
        self, control_plane: TestClient, charts: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clusters = FakeClusters({"cell-4": "stopped"})
        assert (
            run(control_plane, "cells", "deploy", "cell-4", clusters=clusters, charts=charts) == 1
        )
        assert clusters.calls == []
        assert "cell_cluster_exists" in capsys.readouterr().err

    def test_usage_errors(
        self,
        control_plane: TestClient,
        tmp_path: Path,
        charts: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clusters = FakeClusters()
        assert (
            run(control_plane, "cells", "deploy", "Cell_4", clusters=clusters, charts=charts) == 2
        )
        missing = tmp_path / "nope"
        assert (
            run(control_plane, "cells", "deploy", "cell-4", clusters=clusters, charts=missing) == 2
        )
        assert "chart not found" in capsys.readouterr().err
        assert clusters.calls == []

    def test_creates_a_cluster_installs_the_chart_then_registers(
        self, control_plane: TestClient, charts: Path
    ) -> None:
        # The fake cells don't know the generated credential, so registration is rejected
        # at the end; the steps before it, and the cleanup, are what this test checks.
        clusters = FakeClusters({"platform": "running", "cell-1": "running"})
        code = run(
            control_plane,
            "cells", "deploy", "cell-4", "--image", "reg/cell-gateway:1.2",
            clusters=clusters, charts=charts,
        )  # fmt: skip
        assert code == 1
        assert clusters.kinds()[:4] == ["cluster", "import", "helm", "dns"]
        assert clusters.calls[1] == (
            "import",
            "cell-4",
            ("reg/cell-gateway:1.2", "cellarch/user-account:dev", "cellarch/order-processing:dev"),
        )
        helm = clusters.calls[2]
        assert (helm[1], helm[2], helm[3]["cellId"]) == ("cell-4", "cell", "cell-4")
        assert len(helm[3]["secrets"]["internalToken"]) >= 32  # generated
        assert clusters.calls[3] == ("dns", ("cell-1", "cell-4"))
        # Registration failed, so the new cluster was removed again.
        assert clusters.calls[4] == ("delete", "cell-4")
        assert clusters.calls[-1] == ("dns", ("cell-1",))

    def test_cluster_failure_cleans_up(
        self, control_plane: TestClient, charts: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clusters = FakeClusters(fail_on="helm")
        assert (
            run(control_plane, "cells", "deploy", "cell-4", clusters=clusters, charts=charts) == 1
        )
        assert ("delete", "cell-4") in clusters.calls
        assert "helm failed" in capsys.readouterr().err

    def test_warns_when_cleanup_also_fails(
        self, control_plane: TestClient, charts: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class Broken(FakeClusters):
            def delete(self, short: str) -> None:
                raise ClusterError("delete failed")

        assert (
            run(
                control_plane,
                "cells",
                "deploy",
                "cell-4",
                clusters=Broken(fail_on="helm"),
                charts=charts,
            )
            == 1
        )
        assert "cleanup incomplete" in capsys.readouterr().err


class TestDecommission:
    def test_refused_while_tenants_remain(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        run(
            control_plane, "tenants", "create", "acme", clusters=FakeClusters({"cell-1": "running"})
        )
        clusters = FakeClusters({"cell-1": "running"})
        assert run(control_plane, "cells", "decommission", "cell-1", clusters=clusters) == 1
        assert clusters.calls == []
        assert "cell_not_empty" in capsys.readouterr().err

    def test_without_a_local_cluster_only_deregisters(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        clusters = FakeClusters()
        assert run(control_plane, "cells", "decommission", "cell-1", clusters=clusters) == 0
        assert clusters.calls == []
        assert "no local cluster" in capsys.readouterr().err

    def test_stops_the_cluster_and_keeps_its_data_by_default(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        clusters = FakeClusters({"platform": "running", "cell-1": "running", "cell-2": "running"})
        capsys.readouterr()
        assert run(control_plane, "cells", "decommission", "cell-1", clusters=clusters) == 0
        assert clusters.calls == [("stop", "cell-1"), ("dns", ("cell-2",))]
        assert json.loads(capsys.readouterr().out)["cluster"] == "stopped"

    def test_delete_data_deletes_the_cluster(self, control_plane: TestClient) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        clusters = FakeClusters({"cell-1": "running"})
        assert (
            run(
                control_plane, "cells", "decommission", "cell-1", "--delete-data", clusters=clusters
            )
            == 0
        )
        assert clusters.calls == [("delete", "cell-1"), ("dns", ())]

    def test_cluster_errors_are_reported(
        self, control_plane: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(control_plane, "cells", "register", "cell-1", "http://cell-1")
        clusters = FakeClusters({"cell-1": "running"}, fail_on="stop")
        assert run(control_plane, "cells", "decommission", "cell-1", clusters=clusters) == 1
        assert "stop failed" in capsys.readouterr().err


def test_deploy_reports_control_plane_errors_before_touching_clusters(
    charts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthenticated", "message": "bad token"})

    client = httpx.Client(transport=httpx.MockTransport(unauthorized), base_url="http://cp")
    clusters = FakeClusters()
    code = main(
        ["--charts-dir", str(charts), "cells", "deploy", "cell-4"],
        client=client,
        token="wrong",
        clusters=clusters,
    )
    assert code == 1
    assert clusters.calls == []
    assert "unauthenticated" in capsys.readouterr().err


class ScriptedControlPlane:
    """Admin API whose registration answers follow a script (e.g. DNS still settling)."""

    def __init__(self, registrations: list[tuple[int, str]]) -> None:
        self.registrations = list(registrations)
        self.attempts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/cells/cell-4":
            return httpx.Response(404, json={"error": "cell_not_found", "message": "no"})
        if request.method == "POST" and request.url.path == "/v1/cells":
            self.attempts += 1
            status, error = self.registrations.pop(0)
            if status == 201:
                return httpx.Response(201, json={"cell_id": "cell-4", "status": "active"})
            return httpx.Response(status, json={"error": error, "message": "scripted"})
        return httpx.Response(404, json={"error": "not_found", "message": request.url.path})

    def deploy(self, charts: Path, clusters: Any, sleeps: list[float]) -> int:
        client = httpx.Client(transport=httpx.MockTransport(self.handler), base_url="http://cp")
        return main(
            ["--charts-dir", str(charts), "cells", "deploy", "cell-4"],
            client=client,
            token=ADMIN,
            clusters=clusters,
            sleep=sleeps.append,
        )


class TestRegistrationWaitsForCellDns:
    UNREACHABLE = (422, "cell_unreachable")

    def test_retries_while_the_new_cell_name_is_not_resolvable_yet(self, charts: Path) -> None:
        api = ScriptedControlPlane([self.UNREACHABLE, self.UNREACHABLE, (201, "")])
        clusters = FakeClusters({"platform": "running"})
        sleeps: list[float] = []
        assert api.deploy(charts, clusters, sleeps) == 0
        assert api.attempts == 3
        assert len(sleeps) == 2
        assert ("delete", "cell-4") not in clusters.calls

    def test_gives_up_and_cleans_up(self, charts: Path, capsys: pytest.CaptureFixture[str]) -> None:
        api = ScriptedControlPlane([self.UNREACHABLE] * 50)
        clusters = FakeClusters({"platform": "running"})
        sleeps: list[float] = []
        assert api.deploy(charts, clusters, sleeps) == 1
        assert sum(sleeps) >= 60  # waited long enough for DNS to settle
        assert ("delete", "cell-4") in clusters.calls
        assert "cell_unreachable" in capsys.readouterr().err

    def test_other_rejections_are_not_retried(self, charts: Path) -> None:
        api = ScriptedControlPlane([(422, "cell_credential_rejected")])
        clusters = FakeClusters({"platform": "running"})
        sleeps: list[float] = []
        assert api.deploy(charts, clusters, sleeps) == 1
        assert (api.attempts, sleeps) == (1, [])


class TestNewCellsGetMgr:
    """A new cell gets the web UI's backend at the version this environment runs."""

    def test_installs_the_recorded_mgr_version(self, charts: Path) -> None:
        from cellarch.cli.versions import write_version

        write_version(charts.parent, "mgr", "0.4.2-abc1234")
        clusters, sleeps = FakeClusters({"platform": "running"}), list[float]()
        assert ScriptedControlPlane([(201, "")]).deploy(charts, clusters, sleeps) == 0
        assert ("import", "cell-4", ("cellarch/mgr:0.4.2-abc1234",)) in clusters.calls
        mgr = next(c for c in clusters.calls if c[0] == "helm" and c[2] == "mgr")
        assert mgr[3] == {"cellId": "cell-4", "image": {"tag": "0.4.2-abc1234"}}

    def test_without_a_recorded_version_it_says_how_to_add_it(
        self, charts: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clusters, sleeps = FakeClusters({"platform": "running"}), list[float]()
        assert ScriptedControlPlane([(201, "")]).deploy(charts, clusters, sleeps) == 0
        assert not any(c[0] == "helm" and c[2] == "mgr" for c in clusters.calls)
        assert "make deploy-mgr" in capsys.readouterr().err
