"""cellctl: admin CLI for the control plane (docs/plan.md §2.3).

Cell lifecycle
    cellctl cells deploy cell-4 --capacity 10        start a cell container and register it
    cellctl cells list | get cell-4
    cellctl cells set-status cell-4 draining
    cellctl cells set-capacity cell-4 20
    cellctl cells decommission cell-4 [--delete-data]
    cellctl cells register cell-5 http://cell-5:8000 --credential <token>   (deployed elsewhere)

Tenant lifecycle
    cellctl tenants create acme --name "Acme" --tier premium
    cellctl tenants list | get acme

Overview
    cellctl status

Configuration: --url / CELLCTL_URL (default http://localhost:8000), --token / CELLCTL_TOKEN
(the admin credential) and, for deploy/decommission, --compose-file / CELLCTL_COMPOSE_FILE
(default deploy/docker-compose.yml). On AWS, stage 5 adds IAM signing and stage 6 makes
deploy trigger the cell-lifecycle workflow.
"""

import argparse
import getpass
import json
import os
import re
import secrets
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from cellarch.cli.docker import (
    DEFAULT_IMAGE,
    DockerError,
    DockerRunner,
    LocalDocker,
    cell_compose_file,
    cell_compose_spec,
    cell_url,
    volume_name,
)
from cellarch.common.contracts import is_valid_tenant_id
from cellarch.common.dotenv import load_env_file
from cellarch.common.headers import ACTOR_HEADER, INTERNAL_TOKEN_HEADER

DEFAULT_URL = "http://localhost:8000"
_UNSAFE_ACTOR_CHARS = re.compile(r"[^A-Za-z0-9._@-]+")
DEFAULT_COMPOSE_FILE = "deploy/docker-compose.yml"
EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2


class CommandError(Exception):
    """An API call or step failed; reported as ``error: <code>: <message>``."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class UsageError(Exception):
    """Invalid input detected after argument parsing (exit code 2)."""


def make_client(url: str) -> httpx.Client:
    return httpx.Client(base_url=url, timeout=30.0)


def default_actor() -> str | None:
    """The OS user name, made safe for the X-Actor header (recorded as created_by)."""
    try:
        user = getpass.getuser()
    except Exception:  # getpass raises OSError/KeyError when no user name is available
        return None
    return _UNSAFE_ACTOR_CHARS.sub("-", user).strip("-")[:64] or None


class Api:
    def __init__(self, http: httpx.Client, token: str, actor: str | None = None) -> None:
        self._http = http
        self._headers = {INTERNAL_TOKEN_HEADER: token}
        if actor:
            self._headers[ACTOR_HEADER] = actor

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = self._http.request(method, path, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise CommandError("unreachable", f"cannot reach control plane: {exc}") from exc
        data = response.json() if response.content else None
        if response.status_code >= 400:
            info = data if isinstance(data, dict) else {}
            raise CommandError(
                str(info.get("error", "error")),
                str(info.get("message", response.text)),
                response.status_code,
            )
        return data


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
    return settings


def _parser(settings: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cellctl", description="Control plane admin CLI")
    parser.add_argument("--url", default=settings.get("CELLCTL_URL", DEFAULT_URL))
    parser.add_argument("--token", default=settings.get("CELLCTL_TOKEN"))
    parser.add_argument(
        "--actor",
        default=settings.get("CELLCTL_ACTOR"),
        help="your name, recorded as created_by/updated_by (default: your OS user name)",
    )
    parser.add_argument(
        "--compose-file",
        default=settings.get("CELLCTL_COMPOSE_FILE", DEFAULT_COMPOSE_FILE),
        help="local compose stack used by `cells deploy` and `cells decommission`",
    )
    resources = parser.add_subparsers(dest="resource", required=True)

    cells = resources.add_parser("cells", help="deploy and manage cells").add_subparsers(
        dest="action", required=True
    )
    cells.add_parser("list", help="list cells")
    cells.add_parser("get", help="show one cell").add_argument("cell_id")

    deploy = cells.add_parser("deploy", help="start a new cell container and register it")
    deploy.add_argument("cell_id")
    _cell_options(deploy)
    deploy.add_argument("--image", default=DEFAULT_IMAGE, help=f"default: {DEFAULT_IMAGE}")

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
        "--isolation",
        choices=["isolated", "pooled"],
        help="isolated: own database in its cell (default); pooled: shared tables",
    )

    resources.add_parser("status", help="platform capacity overview and warnings")
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
        case _:  # ("tenants", "create") - argparse guarantees nothing else reaches here
            body = {"tenant_id": args.tenant_id, "name": args.name or args.tenant_id}
            body["tier"] = args.tier
            if args.isolation:  # otherwise the control plane's default applies
                body["isolation"] = args.isolation
            return "POST", "/v1/tenants", body


# --- cell deploy / decommission (local Docker) ------------------------------------------


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def _deploy(args: argparse.Namespace, api: Api, docker: DockerRunner) -> Any:
    cell_id = args.cell_id
    if not is_valid_tenant_id(cell_id):
        raise UsageError(
            f"invalid cell id {cell_id!r}: use 2-63 lowercase letters, digits and hyphens"
        )
    compose_file = Path(args.compose_file)
    if not compose_file.is_file():
        raise UsageError(
            f"compose file not found: {compose_file} (run from the repo root or pass "
            "--compose-file)"
        )
    try:
        api.call("GET", f"/v1/cells/{cell_id}")
    except CommandError as exc:
        if exc.status != 404:
            raise
    else:
        raise CommandError("cell_exists", f"cell {cell_id} is already registered", 409)

    credential = secrets.token_urlsafe(32)
    spec_path = cell_compose_file(compose_file, cell_id)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        json.dumps(cell_compose_spec(cell_id, credential, args.image), indent=2),
        encoding="utf-8",
    )
    files = [compose_file, spec_path]
    volume_existed = docker.volume_exists(volume_name(cell_id))

    try:
        _progress(f"Starting container {cell_id} and waiting until it is healthy...")
        docker.compose(files, "up", "--detach", "--wait", "--wait-timeout", "180", cell_id)
        _progress(f"Registering {cell_id} with the control plane...")
        cell = api.call(
            "POST", "/v1/cells", {**_cell_body(args, cell_url(cell_id)), "credential": credential}
        )
    except (CommandError, DockerError):
        _progress(f"Deploy failed; removing {cell_id}...")
        try:
            docker.compose(files, "rm", "--stop", "--force", cell_id)
            if not volume_existed:
                docker.remove_volume(volume_name(cell_id))
        except DockerError as cleanup_error:
            _progress(f"warning: cleanup incomplete: {cleanup_error}")
        spec_path.unlink(missing_ok=True)
        raise
    _progress(f"Cell {cell_id} is deployed and ready for tenants.")
    return cell


def _decommission(args: argparse.Namespace, api: Api, docker: DockerRunner) -> Any:
    cell_id = args.cell_id
    compose_file = Path(args.compose_file)
    spec_path = cell_compose_file(compose_file, cell_id)

    api.call("DELETE", f"/v1/cells/{cell_id}")
    _progress(f"{cell_id} deregistered from the control plane.")
    if not spec_path.is_file():
        _progress(
            f"note: {cell_id} was not deployed by cellctl; stop its container"
            + (" and delete its data" if args.delete_data else "")
            + " yourself."
        )
        return {"cell_id": cell_id, "decommissioned": True, "container_removed": False}

    docker.compose([compose_file, spec_path], "rm", "--stop", "--force", cell_id)
    if args.delete_data:
        docker.remove_volume(volume_name(cell_id))
    spec_path.unlink()
    _progress(f"Container {cell_id} removed" + (" with its data." if args.delete_data else "."))
    return {
        "cell_id": cell_id,
        "decommissioned": True,
        "container_removed": True,
        "data_deleted": args.delete_data,
    }


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
    header = ["TENANT", "NAME", "TIER", "ISOLATION", "CELL", "STATUS", "CREATED BY"]
    rows = [
        [
            t["tenant_id"],
            t["name"],
            t["tier"],
            t["isolation"],
            t["cell_id"],
            t["status"],
            t["created_by"],
        ]
        for t in data
    ]
    return _table([header, *rows])


def main(
    argv: Sequence[str] | None = None,
    *,
    client: httpx.Client | None = None,
    token: str | None = None,
    docker: DockerRunner | None = None,
) -> int:
    try:
        settings = _settings()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    args = _parser(settings).parse_args(argv)

    token = token or args.token
    if not token:
        print("error: set CELLCTL_TOKEN, pass --token, or add ADMIN_TOKEN to .env", file=sys.stderr)
        return EXIT_USAGE

    api = Api(client or make_client(args.url), token, args.actor or default_actor())
    action = (args.resource, getattr(args, "action", None))
    try:
        if action == ("cells", "deploy"):
            data = _deploy(args, api, docker or LocalDocker())
        elif action == ("cells", "decommission"):
            data = _decommission(args, api, docker or LocalDocker())
        else:
            data = api.call(*_request(args))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CommandError as exc:
        status = f" (HTTP {exc.status})" if exc.status else ""
        print(f"error: {exc.code}: {exc.message}{status}", file=sys.stderr)
        return EXIT_ERROR
    except DockerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(_render(args, data))
    return EXIT_OK


def run() -> None:  # pragma: no cover - console-script entry point
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover - `python -m cellarch.cli.cellctl`
    run()
