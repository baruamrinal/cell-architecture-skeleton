# cell-architecture-skeleton

A foundational reference implementation of **cell-based architecture** for a multi-tenant SaaS platform.

Tenants are partitioned into independent **cells**, each a complete copy of the application with its own database. A thin **router** sends each tenant's requests to its cell. A **control plane**, which is never on the request path, decides where tenants live. A failure, bad deploy or noisy tenant affects only one cell.

```
client ──X-Tenant-ID──► router ──► cell-1  (app + own DB)   ← shared by many tenants
                          │    └─► cell-2  (app + own DB)
                          ▼ route lookups (cached)
                     control plane (cells, tenants, placement)
```

The full design is in [docs/plan.md](docs/plan.md): placement, rate limiting, tenant moves, seasonal cells, customer-hosted cells, AWS infrastructure, CI/CD and security guardrails.

## Status

**Stage 1, the core platform, is done.** It covers:
- **Cells:** a tenant-scoped API, and cells reject tenants they don't host.
- **Control plane:** registry, fill-first placement, and separate admin, router and per-cell credentials.
- **Router:**
  - routes each tenant with a cache;
  - keeps serving known tenants when the control plane is down;
  - refreshes stale routes;
  - never exposes `/internal`.
- **Tooling:** the `cellctl` CLI, a Docker image and a local compose stack.

Later stages are listed in [docs/plan.md §13](docs/plan.md).

## Quick start

Requires Docker, plus Python 3.12 for the demo and CLI.

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1    # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

docker compose -f deploy/docker-compose.yml up --build -d --wait
python scripts/demo.py                              # onboard tenants, route, check isolation
```

**Managing tenants and cells:** use the `cellctl` admin CLI; see [Admin CLI](#admin-cli-cellctl) below. The control plane's admin API is on `localhost:8000`, with API docs at `/docs`.

**Calling the tenant API through the router** (`localhost:8080`):
```powershell
curl -H "X-Tenant-ID: acme" -H "Content-Type: application/json" -d '{\"name\":\"first\"}' http://localhost:8080/v1/items
curl -H "X-Tenant-ID: acme" http://localhost:8080/v1/items
```

Stop and remove everything with `docker compose -f deploy/docker-compose.yml down -v`.

## Admin CLI (`cellctl`)

`cellctl` manages tenants and cells through the control plane's admin API. It's installed into the virtual environment by `pip install -e ".[dev]"`.

### Setup

```powershell
.venv\Scripts\Activate.ps1                 # or call it by path: .venv\Scripts\cellctl
$env:CELLCTL_TOKEN = "dev-admin-token"     # admin credential (local development default)
$env:CELLCTL_URL   = "http://localhost:8000"   # optional; this is the default
```

Both settings can also be passed per command: `cellctl --url http://localhost:8000 --token <token> tenants list`.

If `cellctl` is "not recognized", the virtual environment isn't active in that terminal. If activation fails with "running scripts is disabled", run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### Tenant commands

| Command | What it does |
|---|---|
| `cellctl tenants create <tenant-id> [--name NAME] [--tier standard\|premium]` | Onboard a tenant: the control plane picks a cell (fill-first), sets the tenant up there and returns the record. `--name` defaults to the tenant ID; `--tier` defaults to `standard`. |
| `cellctl tenants list` | List all tenants with their tier, cell and status. |
| `cellctl tenants get <tenant-id>` | Show one tenant's full record as JSON. |

Tenant IDs are 2–63 characters: lowercase letters, digits and hyphens, starting with a letter or digit (for example `acme`, `acme-eu`, `t-42`).

**Examples**
```powershell
cellctl tenants create acme --name "Acme Corp" --tier premium
cellctl tenants create globex
cellctl tenants list
cellctl tenants get acme
```

```text
TENANT  NAME       TIER      CELL    STATUS
acme    Acme Corp  premium   cell-1  active
globex  globex     standard  cell-1  active
```

**Possible errors when creating a tenant**

| Error | Meaning |
|---|---|
| `tenant_exists` | A tenant with that ID already exists. |
| `no_capacity` | Every shared, active cell is full. Register a cell or raise a cell's capacity. |
| `provisioning_failed` | The chosen cell couldn't set the tenant up (for example, it's down). Nothing is left behind; retry once the cell is healthy. |
| `validation_error` | The tenant ID or tier isn't valid. |

### Cell commands

Tenants are placed on cells, so these are often needed alongside the tenant commands.

| Command | What it does |
|---|---|
| `cellctl cells list` | List cells with type, status, tenant count, capacity and utilization. |
| `cellctl cells get <cell-id>` | Show one cell as JSON. |
| `cellctl cells register <cell-id> <url> [--capacity N] [--type shared\|dedicated] [--size small\|medium\|large] [--wave N]` | Register a cell. Its credential must already be configured in the control plane (`CELL_CREDENTIALS`), otherwise this fails with `missing_cell_credential`. |
| `cellctl cells set-status <cell-id> active\|draining\|disabled` | `draining` or `disabled` stops new tenants from being placed on the cell; existing tenants keep working. |

```powershell
cellctl cells list
cellctl cells set-status cell-1 draining   # new tenants now go elsewhere
cellctl cells set-status cell-1 active
```

### Output and exit codes

- **Output:** lists print as tables; single records print as JSON.
- **Exit codes:**
  - `0` on success;
  - `1` when the control plane returns an error or can't be reached (the message goes to stderr);
  - `2` for usage errors, such as a missing token or an invalid option.

Moving tenants between cells and seasonal schedules are added to `cellctl` in later stages (3 and 4).

## Development

The project is built **test-first** (TDD, [docs/plan.md §14](docs/plan.md)). The whole platform runs in-process for tests, with no Docker needed.

```powershell
pytest --cov                 # unit + acceptance tests, ≥ 90% branch coverage gate
ruff check . ; ruff format --check .
mypy                         # strict
```

- **Branches and titles:** use a branch per change, and give the PR a conventional-commit title (`feat: …`, `fix(router): …`). PRs are squash-merged.
- **Approval:** `github-actions[bot]` approves PRs through a policy gate when CI is green and the TDD rule holds ([docs/plan.md §9.1](docs/plan.md)).

## Layout

```
src/cellarch/
  common/                  contracts, headers, role-scoped auth, errors, db helpers
  control_plane/           registry, fill-first placement, onboarding, admin + routing APIs
  data_plane/router/       tenant routing, route cache with stale fallback, proxy
  data_plane/cell/         tenant-scoped store and API, /internal provisioning
  cli/                     cellctl
tests/acceptance/          stage acceptance tests against the in-process platform
tests/unit/                unit tests per component
scripts/demo.py            end-to-end walkthrough (also the compose smoke test)
scripts/ci/pr_gate.py      PR policy gate
deploy/docker-compose.yml  local platform
Dockerfile                 one image for every component
```

## License

MIT
