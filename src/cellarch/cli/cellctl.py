"""cellctl: admin CLI for the control plane (docs/plan.md §2.3).

Cell lifecycle
    cellctl cells deploy cell-4 --capacity 10        start a cell container and register it
    cellctl cells list | get cell-4
    cellctl cells set-status cell-4 draining
    cellctl cells set-capacity cell-4 20
    cellctl cells decommission cell-4 [--delete-data]
    cellctl cells register cell-5 http://cell-5:8000 --credential <token>   (deployed elsewhere)

Tenant lifecycle
    cellctl tenants create acme --name "Acme" --tier premium   place it, start its services
    cellctl tenants delete acme                                 stop its services, delete its data
    cellctl tenants list | get acme

Overview
    cellctl status

Configuration: --url / CELLCTL_URL (default http://localhost:8000), --token / CELLCTL_TOKEN
(the admin credential) and, for deploy, --charts-dir / CELLCTL_CHARTS_DIR (default charts).
Each cell is its own local k3d cluster. On AWS, stage 5 adds IAM signing and stage 6 makes
deploy trigger the cell-lifecycle workflow.
"""

import argparse
import getpass
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from cellarch.cli.api import Api, CommandError, make_client
from cellarch.cli.clusters import (
    ClusterError,
    ClusterToolkit,
    LocalClusters,
    cell_url,
    cluster_name,
)
from cellarch.cli.onboarding import offboard, onboard
from cellarch.common.contracts import is_valid_tenant_id
from cellarch.common.dotenv import load_env_file

DEFAULT_URL = "http://localhost:8000"
DEFAULT_ROUTER_URL = "http://localhost:8080"
_UNSAFE_ACTOR_CHARS = re.compile(r"[^A-Za-z0-9._@-]+")
DEFAULT_CHARTS_DIR = "charts"
EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    """Invalid input detected after argument parsing (exit code 2)."""


def default_actor() -> str | None:
    """The OS user name, made safe for the X-Actor header (recorded as created_by)."""
    try:
        user = getpass.getuser()
    except Exception:  # getpass raises OSError/KeyError when no user name is available
        return None
    return _UNSAFE_ACTOR_CHARS.sub("-", user).strip("-")[:64] or None


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


ENV_FILE = Path(".env")


def _settings() -> dict[str, str]:
    """CELLCTL_* settings: environment variables win over .env in the current folder.

    When no CELLCTL_TOKEN is set anywhere, ADMIN_TOKEN from .env is used, so a
    project folder set up with `make up` needs no extra configuration.
    """
    file_values = load_env_file(ENV_FILE) or {}
    settings = {k: v for k, v in file_values.items() if k.startswith("CELLCTL_")}
    settings.update({k: v for k, v in os.environ.items() if k.startswith("CELLCTL_")})
    if not settings.get("CELLCTL_TOKEN") and file_values.get("ADMIN_TOKEN"):
        settings["CELLCTL_TOKEN"] = file_values["ADMIN_TOKEN"]
    if not settings.get("CELLCTL_METRICS_TOKEN") and file_values.get("ROUTER_METRICS_TOKEN"):
        settings["CELLCTL_METRICS_TOKEN"] = file_values["ROUTER_METRICS_TOKEN"]
    return settings


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return number


def _parser(settings: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cellctl", description="Control plane admin CLI")
    parser.add_argument("--url", default=settings.get("CELLCTL_URL", DEFAULT_URL))
    parser.add_argument("--token", default=settings.get("CELLCTL_TOKEN"))
    parser.add_argument(
        "--router-url",
        default=settings.get("CELLCTL_ROUTER_URL", DEFAULT_ROUTER_URL),
        help=f"router address for `usage` (default {DEFAULT_ROUTER_URL})",
    )
    parser.add_argument(
        "--metrics-token",
        default=settings.get("CELLCTL_METRICS_TOKEN"),
        help="router metrics token for `usage` (default: ROUTER_METRICS_TOKEN from .env)",
    )
    parser.add_argument(
        "--actor",
        default=settings.get("CELLCTL_ACTOR"),
        help="your name, recorded as created_by/updated_by (default: your OS user name)",
    )
    parser.add_argument(
        "--charts-dir",
        default=settings.get("CELLCTL_CHARTS_DIR", DEFAULT_CHARTS_DIR),
        help="Helm charts used by `cells deploy` (default: charts)",
    )
    resources = parser.add_subparsers(dest="resource", required=True)

    cells = resources.add_parser("cells", help="deploy and manage cells").add_subparsers(
        dest="action", required=True
    )
    cells.add_parser("list", help="list cells")
    cells.add_parser("get", help="show one cell").add_argument("cell_id")

    deploy = cells.add_parser("deploy", help="create a new cell cluster and register it")
    deploy.add_argument("cell_id")
    _cell_options(deploy)
    deploy.add_argument("--image", default=GATEWAY_IMAGE, help=f"default: {GATEWAY_IMAGE}")

    register = cells.add_parser("register", help="register a cell deployed some other way")
    register.add_argument("cell_id")
    register.add_argument("cell_url", metavar="url")  # distinct dest: --url is the CP address
    _cell_options(register)
    register.add_argument(
        "--credential", help="the cell's CELL_INTERNAL_TOKEN (unless set in CELL_CREDENTIALS)"
    )

    set_status = cells.add_parser("set-status", help="control whether new tenants are placed")
    set_status.add_argument("cell_id")
    set_status.add_argument("status", choices=["active", "draining", "disabled"])

    set_capacity = cells.add_parser("set-capacity", help="change how many tenants a cell holds")
    set_capacity.add_argument("cell_id")
    set_capacity.add_argument("capacity", type=_positive_int)

    decommission = cells.add_parser(
        "decommission", help="deregister an empty cell and remove its container"
    )
    decommission.add_argument("cell_id")
    decommission.add_argument(
        "--delete-data", action="store_true", help="also delete the cell's data volume"
    )

    tenants = resources.add_parser("tenants", help="manage tenants").add_subparsers(
        dest="action", required=True
    )
    tenants.add_parser("list", help="list tenants")
    tenants.add_parser("get", help="show one tenant").add_argument("tenant_id")
    create = tenants.add_parser("create", help="onboard a tenant")
    create.add_argument("tenant_id")
    create.add_argument("--name")
    create.add_argument("--tier", choices=["standard", "premium"], default="standard")
    create.add_argument(
        "--charts-dir", default=settings.get("CELLCTL_CHARTS_DIR", DEFAULT_CHARTS_DIR)
    )
    delete = tenants.add_parser("delete", help="stop a tenant's services and delete its data")
    delete.add_argument("tenant_id")

    set_limit = tenants.add_parser(
        "set-limit", help="override a tenant's rate limit, or --clear to use its tier default"
    )
    set_limit.add_argument("tenant_id")
    set_limit.add_argument("--rps", type=_positive_float, help="requests per second")
    set_limit.add_argument("--burst", type=_positive_int, help="requests allowed at once")
    set_limit.add_argument("--clear", action="store_true", help="remove the override")

    resources.add_parser("status", help="platform capacity overview and warnings")
    resources.add_parser("usage", help="per-tenant request usage, from the router")
    return parser


def _cell_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--capacity", type=_positive_int)
    parser.add_argument("--type", choices=["shared", "dedicated"], default="shared")
    parser.add_argument("--size", choices=["small", "medium", "large"], default="small")
    parser.add_argument("--wave", type=int, default=1)


def _cell_body(args: argparse.Namespace, url: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "cell_id": args.cell_id,
        "url": url,
        "type": args.type,
        "size": args.size,
        "wave": args.wave,
    }
    if args.capacity is not None:
        body["capacity"] = args.capacity
    return body


def _request(args: argparse.Namespace) -> tuple[str, str, dict[str, Any] | None]:
    """Map simple commands to (method, path, body)."""
    match (args.resource, getattr(args, "action", None)):
        case ("status", _):
            return "GET", "/v1/status", None
        case ("cells", "list"):
            return "GET", "/v1/cells", None
        case ("cells", "get"):
            return "GET", f"/v1/cells/{args.cell_id}", None
        case ("cells", "register"):
            body = _cell_body(args, args.cell_url)
            if args.credential:
                body["credential"] = args.credential
            return "POST", "/v1/cells", body
        case ("cells", "set-status"):
            return "PATCH", f"/v1/cells/{args.cell_id}", {"status": args.status}
        case ("cells", "set-capacity"):
            return "PATCH", f"/v1/cells/{args.cell_id}", {"capacity": args.capacity}
        case ("tenants", "list"):
            return "GET", "/v1/tenants", None
        case ("tenants", "get"):
            return "GET", f"/v1/tenants/{args.tenant_id}", None
        case ("tenants", "set-limit"):
            path = f"/v1/tenants/{args.tenant_id}/rate-limit"
            if args.clear:
                return "DELETE", path, None
            return "PUT", path, {"rps": args.rps, "burst": args.burst}
        case _:  # pragma: no cover - argparse guarantees nothing else reaches here
            raise AssertionError(f"unhandled command {args.resource} {args.action}")


# --- cell deploy / decommission (local k3d: one cluster per cell) -----------------------

CELL_RELEASE = "cellarch"
GATEWAY_IMAGE = "cellarch/cell-gateway:dev"
# The control plane may not resolve a new cell's name until cluster DNS settles.
REGISTER_ATTEMPTS, REGISTER_DELAY_SECONDS = 21, 3.0
# The tenants' services run in every cell cluster (tenant-stack chart).
TENANT_IMAGES = ["cellarch/user-account:dev", "cellarch/order-processing:dev"]


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def _sync_cell_dns(clusters: ClusterToolkit) -> None:
    clusters.set_cell_dns({cell: clusters.node_ip(cell) for cell in clusters.cell_ids()})


def _deploy(
    args: argparse.Namespace,
    api: Api,
    clusters: ClusterToolkit,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    cell_id = args.cell_id
    if not is_valid_tenant_id(cell_id):
        raise UsageError(
            f"invalid cell id {cell_id!r}: use 2-63 lowercase letters, digits and hyphens"
        )
    chart = Path(args.charts_dir) / "cell"
    if not chart.is_dir():
        raise UsageError(
            f"chart not found: {chart} (run from the project folder or pass --charts-dir)"
        )
    try:
        api.call("GET", f"/v1/cells/{cell_id}")
    except CommandError as exc:
        if exc.status != 404:
            raise
    else:
        raise CommandError("cell_exists", f"cell {cell_id} is already registered", 409)
    if cell_id in clusters.clusters():
        raise CommandError(
            "cell_cluster_exists",
            f"cluster {cluster_name(cell_id)} already exists; decommission it first",
        )

    credential = secrets.token_urlsafe(32)
    try:
        _progress(f"Creating cluster {cluster_name(cell_id)}...")
        clusters.ensure_cluster(cell_id)
        clusters.import_images(cell_id, [args.image, *TENANT_IMAGES])
        _progress("Installing the cell chart and waiting until the gateway is ready...")
        values = {"cellId": cell_id, "secrets": {"internalToken": credential}}
        clusters.helm_upgrade(cell_id, CELL_RELEASE, chart, values)
        _sync_cell_dns(clusters)
        _progress(f"Registering {cell_id} with the control plane...")
        body = {**_cell_body(args, cell_url(cell_id)), "credential": credential}
        attempt = 1
        while True:
            try:
                cell = api.call("POST", "/v1/cells", body)
                break
            except CommandError as exc:
                if exc.code != "cell_unreachable" or attempt == REGISTER_ATTEMPTS:
                    raise
                attempt += 1
                sleep(REGISTER_DELAY_SECONDS)
    except (CommandError, ClusterError):
        _progress(f"Deploy failed; removing cluster {cluster_name(cell_id)}...")
        try:
            clusters.delete(cell_id)
            _sync_cell_dns(clusters)
        except ClusterError as cleanup_error:
            _progress(f"warning: cleanup incomplete: {cleanup_error}")
        raise
    _progress(f"Cell {cell_id} is deployed in its own cluster and ready for tenants.")
    return cell


def _decommission(args: argparse.Namespace, api: Api, clusters: ClusterToolkit) -> Any:
    cell_id = args.cell_id
    api.call("DELETE", f"/v1/cells/{cell_id}")
    _progress(f"{cell_id} deregistered from the control plane.")
    if cell_id not in clusters.clusters():
        _progress(f"note: no local cluster {cluster_name(cell_id)}; nothing else to remove.")
        return {"cell_id": cell_id, "decommissioned": True, "cluster": "none"}

    if args.delete_data:
        clusters.delete(cell_id)
        outcome = "deleted"
    else:
        clusters.stop(cell_id)
        outcome = "stopped"
    # A stopped cluster is still listed, so leave this cell out of the DNS zone explicitly.
    remaining = [cell for cell in clusters.cell_ids() if cell != cell_id]
    clusters.set_cell_dns({cell: clusters.node_ip(cell) for cell in remaining})
    _progress(
        f"Cluster {cluster_name(cell_id)} "
        + ("deleted with its data." if outcome == "deleted" else "stopped; its data is kept.")
    )
    return {"cell_id": cell_id, "decommissioned": True, "cluster": outcome}


# --- output -----------------------------------------------------------------------------


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _cells_table(cells: list[dict[str, Any]]) -> str:
    header = ["CELL", "TYPE", "STATUS", "TENANTS", "CAPACITY", "UTILIZATION", "CREATED BY", "URL"]
    rows = [
        [
            c["cell_id"],
            c["type"],
            c["status"],
            str(c["tenant_count"]),
            str(c["capacity"]),
            f"{c['utilization'] * 100:.0f}%",
            c["created_by"],
            c["url"],
        ]
        for c in cells
    ]
    return _table([header, *rows])


def _status(data: dict[str, Any]) -> str:
    totals = data["totals"]
    lines = [
        f"Target utilization: {data['target_utilization']:.0%}",
        f"Cells: {totals['cells']} ({totals['placeable_cells']} placeable)   "
        f"Tenants: {totals['tenants']}   Capacity: {totals['capacity']}   "
        f"Free slots: {totals['free_slots']}",
        "",
    ]
    if data["cells"]:
        lines.extend([_cells_table(data["cells"]), ""])
    if data["warnings"]:
        lines.append("Warnings:")
        lines.extend(f"  {w['code']}: {w['message']}" for w in data["warnings"])
    else:
        lines.append("No warnings.")
    return "\n".join(lines)


def _render(args: argparse.Namespace, data: Any) -> str:
    if args.resource == "status":
        return _status(data)
    if not isinstance(data, list):
        return json.dumps(data, indent=2)
    if args.resource == "cells":
        return _cells_table(data)
    header = ["TENANT", "NAME", "TIER", "CELL", "STATUS", "CREATED BY"]
    rows = [
        [
            t["tenant_id"],
            t["name"],
            t["tier"],
            t["cell_id"],
            t["status"],
            t["created_by"],
        ]
        for t in data
    ]
    return _table([header, *rows])


def _usage_table(data: dict[str, Any]) -> str:
    lines = [f"Usage since {data['since']} (this router; {data['router_replicas']} replica(s))", ""]
    if not data["tenants"]:
        return "\n".join([*lines, "No tenant traffic yet."])
    rows = [["TENANT", "REQUESTS", "THROTTLED", "2XX", "4XX", "5XX"]]
    for t in data["tenants"]:
        share = round(100 * t["throttled"] / t["requests"]) if t["requests"] else 0
        responses = t["responses"]
        rows.append(
            [
                t["tenant_id"],
                str(t["requests"]),
                f"{t['throttled']} ({share}%)",
                str(responses["2xx"]),
                str(responses["4xx"]),
                str(responses["5xx"]),
            ]
        )
    return "\n".join([*lines, _table(rows)])


def _usage(args: argparse.Namespace, router_client: httpx.Client | None) -> int:
    if not args.metrics_token:
        print(
            "error: set CELLCTL_METRICS_TOKEN, pass --metrics-token, "
            "or add ROUTER_METRICS_TOKEN to .env",
            file=sys.stderr,
        )
        return EXIT_USAGE
    api = Api(router_client or make_client(args.router_url), args.metrics_token)
    try:
        data = api.call("GET", "/_router/stats")
    except CommandError as exc:
        status = f" (HTTP {exc.status})" if exc.status else ""
        print(f"error: {exc.code}: {exc.message}{status}", file=sys.stderr)
        return EXIT_ERROR
    print(_usage_table(data))
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    client: httpx.Client | None = None,
    token: str | None = None,
    clusters: ClusterToolkit | None = None,
    router_client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    try:
        settings = _settings()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    parser = _parser(settings)
    args = parser.parse_args(argv)
    if getattr(args, "action", None) == "set-limit":
        has_values = args.rps is not None or args.burst is not None
        if args.clear == has_values or (has_values and (args.rps is None or args.burst is None)):
            parser.error("set-limit needs both --rps and --burst, or --clear on its own")

    if args.resource == "usage":  # served by the router, with its metrics token
        return _usage(args, router_client)

    token = token or args.token
    if not token:
        print("error: set CELLCTL_TOKEN, pass --token, or add ADMIN_TOKEN to .env", file=sys.stderr)
        return EXIT_USAGE

    api = Api(client or make_client(args.url), token, args.actor or default_actor())
    action = (args.resource, getattr(args, "action", None))
    try:
        if action == ("cells", "deploy"):
            data = _deploy(args, api, clusters or LocalClusters(), sleep)
        elif action == ("cells", "decommission"):
            data = _decommission(args, api, clusters or LocalClusters())
        elif action == ("tenants", "create"):
            body = {"tenant_id": args.tenant_id, "name": args.name or args.tenant_id}
            body["tier"] = args.tier
            data = onboard(
                api, clusters or LocalClusters(), Path(args.charts_dir), body, progress=_progress
            )
        elif action == ("tenants", "delete"):
            offboard(api, clusters or LocalClusters(), args.tenant_id, progress=_progress)
            return EXIT_OK
        else:
            data = api.call(*_request(args))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CommandError as exc:
        status = f" (HTTP {exc.status})" if exc.status else ""
        print(f"error: {exc.code}: {exc.message}{status}", file=sys.stderr)
        return EXIT_ERROR
    except ClusterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(_render(args, data))
    return EXIT_OK


def run() -> None:  # pragma: no cover - console-script entry point
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover - `python -m cellarch.cli.cellctl`
    run()
