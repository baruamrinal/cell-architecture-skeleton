"""cellctl: admin CLI for the control plane (docs/plan.md §2.3).

    cellctl cells list
    cellctl cells register cell-3 http://cell-3:8000 --capacity 50
    cellctl cells set-status cell-1 draining
    cellctl tenants create acme --name "Acme" --tier premium
    cellctl tenants list

Configuration: --url / CELLCTL_URL (default http://localhost:8000) and
--token / CELLCTL_TOKEN (the admin credential). On AWS, stage 5 adds IAM signing.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

import httpx

from cellarch.common.headers import INTERNAL_TOKEN_HEADER

DEFAULT_URL = "http://localhost:8000"
EXIT_OK, EXIT_API_ERROR, EXIT_USAGE = 0, 1, 2


def make_client(url: str) -> httpx.Client:
    return httpx.Client(base_url=url, timeout=30.0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cellctl", description="Control plane admin CLI")
    parser.add_argument("--url", default=os.environ.get("CELLCTL_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("CELLCTL_TOKEN"))
    resources = parser.add_subparsers(dest="resource", required=True)

    cells = resources.add_parser("cells", help="manage cells").add_subparsers(
        dest="action", required=True
    )
    cells.add_parser("list")
    cells.add_parser("get").add_argument("cell_id")
    register = cells.add_parser("register")
    register.add_argument("cell_id")
    register.add_argument("cell_url", metavar="url")  # distinct dest: --url is the CP address
    register.add_argument("--capacity", type=int)
    register.add_argument("--type", choices=["shared", "dedicated"], default="shared")
    register.add_argument("--size", choices=["small", "medium", "large"], default="small")
    register.add_argument("--wave", type=int, default=1)
    set_status = cells.add_parser("set-status")
    set_status.add_argument("cell_id")
    set_status.add_argument("status", choices=["active", "draining", "disabled"])

    tenants = resources.add_parser("tenants", help="manage tenants").add_subparsers(
        dest="action", required=True
    )
    tenants.add_parser("list")
    tenants.add_parser("get").add_argument("tenant_id")
    create = tenants.add_parser("create")
    create.add_argument("tenant_id")
    create.add_argument("--name")
    create.add_argument("--tier", choices=["standard", "premium"], default="standard")
    return parser


def _request(args: argparse.Namespace) -> tuple[str, str, dict[str, Any] | None]:
    """Map parsed arguments to (method, path, body)."""
    match (args.resource, args.action):
        case ("cells", "list"):
            return "GET", "/v1/cells", None
        case ("cells", "get"):
            return "GET", f"/v1/cells/{args.cell_id}", None
        case ("cells", "register"):
            body = {
                "cell_id": args.cell_id,
                "url": args.cell_url,
                "type": args.type,
                "size": args.size,
                "wave": args.wave,
            }
            if args.capacity is not None:
                body["capacity"] = args.capacity
            return "POST", "/v1/cells", body
        case ("cells", "set-status"):
            return "PATCH", f"/v1/cells/{args.cell_id}", {"status": args.status}
        case ("tenants", "list"):
            return "GET", "/v1/tenants", None
        case ("tenants", "get"):
            return "GET", f"/v1/tenants/{args.tenant_id}", None
        case _:  # ("tenants", "create") - argparse guarantees nothing else reaches here
            body = {"tenant_id": args.tenant_id, "name": args.name or args.tenant_id}
            return "POST", "/v1/tenants", {**body, "tier": args.tier}


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _render(resource: str, data: Any) -> str:
    if not isinstance(data, list):
        return json.dumps(data, indent=2)
    if resource == "cells":
        header = ["CELL", "TYPE", "STATUS", "TENANTS", "CAPACITY", "UTILIZATION", "URL"]
        rows = [
            [
                c["cell_id"],
                c["type"],
                c["status"],
                str(c["tenant_count"]),
                str(c["capacity"]),
                f"{c['utilization'] * 100:.0f}%",
                c["url"],
            ]
            for c in data
        ]
    else:
        header = ["TENANT", "NAME", "TIER", "CELL", "STATUS"]
        rows = [[t["tenant_id"], t["name"], t["tier"], t["cell_id"], t["status"]] for t in data]
    return _table([header, *rows])


def main(
    argv: Sequence[str] | None = None,
    *,
    client: httpx.Client | None = None,
    token: str | None = None,
) -> int:
    args = _parser().parse_args(argv)

    token = token or args.token
    if not token:
        print("error: set CELLCTL_TOKEN or pass --token", file=sys.stderr)
        return EXIT_USAGE

    method, path, body = _request(args)
    http = client or make_client(args.url)
    try:
        response = http.request(method, path, json=body, headers={INTERNAL_TOKEN_HEADER: token})
    except httpx.HTTPError as exc:
        print(f"error: cannot reach control plane: {exc}", file=sys.stderr)
        return EXIT_API_ERROR

    data = response.json() if response.content else None
    if response.status_code >= 400:
        code = data.get("error", "error") if isinstance(data, dict) else "error"
        message = data.get("message", "") if isinstance(data, dict) else response.text
        print(f"error: {code}: {message} (HTTP {response.status_code})", file=sys.stderr)
        return EXIT_API_ERROR

    print(_render(args.resource, data))
    return EXIT_OK


def run() -> None:  # pragma: no cover - console-script entry point
    sys.exit(main())
