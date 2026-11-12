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

Requires Docker and Python 3.12. From the project folder:

```powershell
. .\make up                 # Windows (PowerShell). The leading dot keeps .venv activated afterwards.
python scripts/demo.py      # onboard tenants, route, check isolation
```
On macOS/Linux: `make up`, then `source .venv/bin/activate`.

`make up` does everything in one step:
1. creates `.venv` and installs the project if needed, then activates it;
2. loads settings from `.env` if it exists;
3. builds the image;
4. starts all containers, **including cells added with `cellctl cells deploy`**;
5. waits until they're healthy;
6. shows `cellctl status`.

| Command | What it does |
|---|---|
| `.\make up` | Set up, build and start everything (rebuilds after code changes) |
| `.\make down` | Stop and remove the containers; data is kept |
| `.\make status` | Platform status (`cellctl status`) |
| `.\make build` | Build the image only |
| `.\make test` | Run the test suite with coverage |
| `.\make setup` | Reinstall the project into `.venv` |

**Settings:** copy [.env.example](.env.example) to `.env` to change tokens or include extra compose files, e.g. `EXTRA_COMPOSE_FILES=deploy/cell-3.override.yml`. `.env` is git-ignored. Without it, development defaults are used.

If PowerShell says "running scripts is disabled", run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

**Managing tenants and cells:** use the `cellctl` admin CLI; see [Admin CLI](#admin-cli-cellctl) below. The control plane's admin API is on `localhost:8000`, with API docs at `/docs`.

**Calling the tenant API through the router** (`localhost:8080`):
```powershell
curl -H "X-Tenant-ID: acme" -H "Content-Type: application/json" -d '{\"name\":\"first\"}' http://localhost:8080/v1/items
curl -H "X-Tenant-ID: acme" http://localhost:8080/v1/items
```

Stop everything with `.\make down`. To also delete all data, run `docker compose -f deploy/docker-compose.yml down -v`.

## Admin CLI (`cellctl`)

`cellctl` covers the whole lifecycle: deploying and managing **cells**, and onboarding and managing **tenants**. It's installed into the virtual environment by `pip install -e ".[dev]"`.

### Setup

```powershell
. .\make up          # activates .venv and sets CELLCTL_TOKEN in this terminal
cellctl status
```

Run `cellctl` from the project folder. It **reads `.env` there**, so it works in any terminal with the virtual environment active, without setting a token by hand. Settings are taken in this order, first match wins:
1. a command-line flag (`--token`, …);
2. an environment variable (`CELLCTL_TOKEN`, …);
3. the same `CELLCTL_*` setting in `.env`;
4. for the token only, `ADMIN_TOKEN` in `.env`.

Without a `.env` file and with no token set, `cellctl` asks for one rather than assuming a development default.

| Setting | Flag | Default |
|---|---|---|
| `CELLCTL_URL` | `--url` | `http://localhost:8000` (control plane admin API) |
| `CELLCTL_TOKEN` | `--token` | `ADMIN_TOKEN` from `.env`; otherwise required |
| `CELLCTL_COMPOSE_FILE` | `--compose-file` | `deploy/docker-compose.yml` (used by `cells deploy` / `decommission`) |
| `CELLCTL_ACTOR` | `--actor` | your OS user name; recorded as who created or changed cells and tenants |

**Who did what:** cells and tenants record `created_by` and `updated_by` as `admin:<your-name>`. They appear in the JSON and in a **CREATED BY** column in `cells list` and `tenants list`. Seeded cells show `config`. Records from before this feature show `unknown`. Locally the name part is self-reported, because everyone shares the admin token. On AWS (stage 5) it becomes the verified IAM identity of the caller.

If `cellctl` is "not recognized", the virtual environment isn't active in that terminal. If activation fails with "running scripts is disabled", run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### Commands at a glance

```text
CELL LIFECYCLE
  cellctl cells deploy <cell-id> [--capacity N] [--type T] [--size S] [--wave N] [--image I]
  cellctl cells list
  cellctl cells get <cell-id>
  cellctl cells set-status <cell-id> active|draining|disabled
  cellctl cells set-capacity <cell-id> <N>
  cellctl cells decommission <cell-id> [--delete-data]
  cellctl cells register <cell-id> <url> [--credential C] [--capacity N] [--type T] [--size S] [--wave N]

TENANT LIFECYCLE
  cellctl tenants create <tenant-id> [--name NAME] [--tier standard|premium]
  cellctl tenants list
  cellctl tenants get <tenant-id>

OVERVIEW
  cellctl status
```

Every command has `--help`, e.g. `cellctl cells deploy --help`.

### Cell lifecycle: deploy → manage → decommission

**1. Deploy a new cell** (local Docker stack running):
```powershell
cellctl cells deploy cell-4 --capacity 10
```
In one step this:
1. generates a random credential for the cell;
2. starts a `cell-4` container on the stack's network;
3. waits until the cell is healthy;
4. registers it with the control plane.

No restart and no hand-edited files are needed. The control plane checks the new cell before accepting it: it must be healthy, report the right cell ID, and accept the credential. If any step fails, the container is removed again and nothing is registered.

| Option | Meaning | Default |
|---|---|---|
| `cell-id` | Unique name: 2–63 lowercase letters, digits or hyphens. | — |
| `--capacity` | Maximum tenants on the cell. Fill-first places tenants up to 75% of it, then uses the remaining room. | Control plane's `DEFAULT_CELL_CAPACITY` (4 locally) |
| `--type` | `shared` for many tenants; `dedicated` is reserved for one tenant and never used for pooled placement. | `shared` |
| `--size` | `small`, `medium` or `large`. A label for now; maps to AWS sizes in stage 5. | `small` |
| `--wave` | Release wave for cell-by-cell rollouts (stage 6); `0` is the canary. | `1` |
| `--image` | Container image for the cell. | `cellarch:dev` (built by `docker compose up --build`) |

The generated compose file is kept in `deploy/cells/<cell-id>.yml`. It contains the cell's credential and is git-ignored.

**2. Manage cells**
```powershell
cellctl cells list                         # tenants, capacity and utilization per cell
cellctl cells get cell-4
cellctl cells set-capacity cell-4 20       # can't go below the cell's current tenant count
cellctl cells set-status cell-4 draining   # no new tenants; existing ones keep working
cellctl cells set-status cell-4 active
```

**3. Decommission a cell**
```powershell
cellctl cells decommission cell-4                 # deregister + remove the container, keep its data
cellctl cells decommission cell-4 --delete-data   # also delete its data volume
```
Decommissioning is refused while the cell still hosts tenants (`cell_not_empty`). Moving tenants off a cell arrives in stage 3. For cells not deployed by `cellctl`, such as `cell-1` and `cell-2` from the compose file, only the registration is removed; stop their containers yourself.

**Registering a cell deployed some other way** (advanced): if you started a cell yourself, register it with its credential, which is the cell's `CELL_INTERNAL_TOKEN`. The cell's `url` is the address the control plane and router use, e.g. `http://cell-5:8000`, not `localhost`.
```powershell
cellctl cells register cell-5 http://cell-5:8000 --credential <the-cell's-token> --capacity 10
```
[deploy/cell-3.override.yml](deploy/cell-3.override.yml) is a hand-written example of such a cell. Its credential is in the control plane's configuration, so `--credential` isn't needed for it:
```powershell
docker compose -f deploy/docker-compose.yml -f deploy/cell-3.override.yml up -d --wait
cellctl cells register cell-3 http://cell-3:8000 --capacity 4
```

### Tenant lifecycle

```powershell
cellctl tenants create acme --name "Acme Corp" --tier premium   # placed on a cell automatically
cellctl tenants create globex                                   # name defaults to the ID
cellctl tenants create trial-42 --isolation pooled              # shares tables (see below)
cellctl tenants list
cellctl tenants get acme
```

```text
TENANT    NAME       TIER      ISOLATION  CELL    STATUS  CREATED BY
acme      Acme Corp  premium   isolated   cell-1  active  admin:mrinal
globex    globex     standard  isolated   cell-1  active  admin:mrinal
trial-42  trial-42   standard  pooled     cell-1  active  admin:mrinal
```

Tenant IDs are 2–63 characters: lowercase letters, digits and hyphens, starting with a letter or digit (for example `acme`, `acme-eu`, `t-42`). `--tier` defaults to `standard`.

**Data isolation:** tenants share cells up to each cell's capacity, but **each tenant gets its own database inside its cell** by default (`--isolation isolated`). In the cell's volume that's `tenants/<tenant-id>.db`.
- `--isolation pooled` stores a tenant in shared tables instead, separated by `tenant_id`. It's meant for free, trial or demo tenants where cost and density matter more.
- Removing an isolated tenant deletes its database.
- Change the default with `DEFAULT_TENANT_ISOLATION` in `.env`.

See [docs/plan.md §3.4](docs/plan.md).

**Rate limits:** the router limits each tenant's requests, so one tenant can't overload a shared cell.

| Tier | Default limit |
|---|---|
| `standard` | 50 requests/s, bursts up to 100 |
| `premium` | 500 requests/s, bursts up to 1000 |

```powershell
cellctl tenants set-limit acme --rps 200 --burst 400   # override for one tenant
cellctl tenants set-limit acme --clear                  # back to the tier default
cellctl tenants get acme                                # "rate_limit": {"rps", "burst", "source": tier|override}
```
- **Over the limit:** the router answers `429 rate_limited` with `Retry-After`, and the request never reaches the cell.
- **Headers:** every response carries `X-RateLimit-Limit` (requests allowed at once), `X-RateLimit-Remaining` and `X-RateLimit-Reset` (seconds until the full allowance is back).
- **When changes apply:** within 5 seconds, when the router refreshes its cached route. Limits keep working while the control plane is down.
- **Tier defaults:** change them with `RATE_LIMIT_STANDARD` / `RATE_LIMIT_PREMIUM` in `.env`.
- **Several routers:** set `ROUTER_REPLICAS`, and each router enforces its share (limit ÷ replicas).

Moving tenants between cells (stage 3) and seasonal schedules (stage 4) will be added here.

### Platform overview

```powershell
cellctl status
```
```text
Target utilization: 75%
Cells: 3 (3 placeable)   Tenants: 9   Capacity: 18   Free slots: 9

CELL    TYPE    STATUS  TENANTS  CAPACITY  UTILIZATION  URL
...

Warnings:
  capacity_low: Every shared active cell is at or past the 75% target; ...
```
**Placeable cells** are shared and active, so they can take new tenants. The status shows two warnings:
- `capacity_low`: every placeable cell is past the 75% target.
- `no_capacity`: new tenants will be refused.

Either one means it's time for `cells deploy` or `cells set-capacity`.

### Errors and exit codes

| Error | Meaning |
|---|---|
| `no_capacity` | No placeable cell has room. Deploy a cell or raise a cell's capacity. |
| `tenant_exists` / `cell_exists` | That ID is already in use. |
| `provisioning_failed` | The chosen cell couldn't set the tenant up (e.g. it's down). Nothing is left behind; retry once it's healthy. |
| `capacity_below_tenants` | Capacity can't be set below the cell's current tenant count. |
| `cell_not_empty` | A cell with tenants can't be decommissioned. |
| `missing_cell_credential` | Registering without `--credential` for a cell whose credential isn't configured. |
| `cell_unreachable` / `cell_id_mismatch` / `cell_credential_rejected` | The cell failed the checks done before it is registered. |
| `validation_error` | An ID or option isn't valid. |

- **Output:** lists print as tables; single records as JSON; progress messages go to stderr.
- **Exit codes:**
  - `0` on success;
  - `1` when the control plane or Docker returns an error, or can't be reached;
  - `2` for usage errors, such as a missing token, an invalid option or a missing compose file.

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
  cli/                     cellctl and its local Docker runner (cells deploy/decommission)
tests/acceptance/          stage acceptance tests against the in-process platform
tests/unit/                unit tests per component
scripts/demo.py            end-to-end walkthrough (also the compose smoke test)
scripts/dev.py             local tasks behind make.ps1 (Windows) and Makefile (macOS/Linux)
scripts/ci/pr_gate.py      PR policy gate
deploy/docker-compose.yml  local platform
Dockerfile                 one image for every component
```

## License

MIT
