# cell-architecture-skeleton

A foundational reference implementation of **cell-based architecture** for a multi-tenant SaaS platform.

Tenants are partitioned into independent **cells**, each a complete copy of the application with its own database. A thin **router** sends each tenant's requests to its cell. A **control plane**, which is never on the request path, decides where tenants live. A failure, bad deploy or noisy tenant affects only one cell.

```
client ──X-Tenant-ID──► router ──► cell-1  (app + own DB)   ← shared by many tenants
                          │    └─► cell-2  (app + own DB)
                          ▼ route lookups (cached)
                     control plane (cells, tenants, placement)
```

It's a skeleton with an **enterprise design**: production differs only in how much is implemented, not in how it's structured. [docs/production-architecture.md](docs/production-architecture.md) shows how each local piece maps to AWS, plus high availability, disaster recovery, security, observability and cost.

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
- **Tooling:** the `cellctl` CLI, one image per component, Helm charts, and a local Kubernetes runtime: a k3d platform cluster plus **one cluster per cell**.

Later stages are listed in [docs/plan.md §13](docs/plan.md).

## Quick start

Requires **Docker**, **Python 3.12**, **[k3d](https://k3d.io)** and **[Helm](https://helm.sh)** (on Windows: `winget install k3d.k3d` and `winget install Helm.Helm`, then open a new terminal). Give Docker Desktop **8 GB or more** of memory; 10–12 GB is recommended. From the project folder:

```powershell
. .\make up                 # Windows (PowerShell). The leading dot keeps .venv activated afterwards.
python scripts/demo.py      # onboard tenants (each with its own services), sign up users, place orders
```
On macOS/Linux: `make up`, then `source .venv/bin/activate`.

The local topology mirrors production, with **every cell in its own cluster**:
```
k3d cluster cellarch-platform   router (localhost:8080), control plane (localhost:8000)
k3d cluster cellarch-cell-1     cell-1's gateway (more cells: more clusters)
                                all on the private Docker network "cellarch"
```
The router and control plane reach each cell by name (`cell-1.cellarch.internal`) over that network. Cell gateways are **not** published on your machine.

`make up` does everything in one step:
1. creates `.venv` and installs the project if needed, then activates it;
2. loads settings from `.env` if it exists;
3. creates or starts the clusters: the platform plus each cell in `CELLS` (default `cell-1`) and any cell added with `cellctl cells deploy`;
4. builds the five images and loads them into the clusters;
5. installs or upgrades the Helm charts and waits until everything is ready;
6. shows `cellctl status`.

| Command | What it does |
|---|---|
| `.\make up` | Set up, build and start everything (rebuilds after code changes) |
| `.\make down` | Stop the clusters; data is kept |
| `.\make clean` | **Delete** the clusters and all their data |
| `.\make status` | Platform status (`cellctl status`) |
| `.\make build` | Build the images only |
| `.\make test` | Run the test suite with coverage |
| `.\make setup` | Reinstall the project into `.venv` |

**Settings:** copy [.env.example](.env.example) to `.env` to change tokens, seed cells (`CELLS`), limits and more. `.env` is git-ignored. Without it, development defaults are used.

**Coming from the Docker Compose version?** Stop the old stack first with `docker compose -p cellarch down`; `make up` refuses to start while it holds ports 8080 and 8000. The k3d clusters start with fresh data. The old compose volumes stay untouched until you remove them (`docker compose -p cellarch down -v`). Files in `deploy/cells/` are no longer used and can be deleted; they contain cell credentials.

If PowerShell says "running scripts is disabled", run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

**Managing tenants and cells:** use the `cellctl` admin CLI; see [Admin CLI](#admin-cli-cellctl) below. The control plane's admin API is on `localhost:8000`, with API docs at `/docs`.

**Calling a tenant's services through the router** (`localhost:8080`), after `cellctl tenants create acme`:
```powershell
curl -H "X-Tenant-ID: acme" -H "Content-Type: application/json" -d '{\"email\":\"ana@acme.test\",\"name\":\"Ana\"}' http://localhost:8080/v1/users
curl -H "X-Tenant-ID: acme" http://localhost:8080/v1/users
curl -H "X-Tenant-ID: acme" -H "Content-Type: application/json" -d '{\"user_id\":\"<id from above>\",\"lines\":[{\"sku\":\"book-1\",\"quantity\":2,\"unit_price_cents\":1250}]}' http://localhost:8080/v1/orders
```
`/v1/users` goes to the tenant's own `user-account` service and `/v1/orders` to its own `order-processing` service. Creates accept an `Idempotency-Key` header; lists return `{"items": [...], "next_cursor": ...}`.

**The web UI's GraphQL API** (stage 4; the `web-ui` front end follows). Every tenant has its own address, `http://<tenant>.localhost:8080` (browsers send `*.localhost` to your machine). Sign in there with the development-only identity provider, then query `/graphql`:
```powershell
# Sign in for acme (the dev identity provider sets the session cookie for acme.localhost)
curl -c acme.cookies -X POST http://acme.localhost:8080/auth/dev/sign-in
# Queries go as GET, mutations as POST with the anti-CSRF header
curl -b acme.cookies "http://acme.localhost:8080/graphql?query=%7Bme%7BtenantId%20cellId%7D%7D"
curl -b acme.cookies -H "X-Cellarch-CSRF: 1" -H "Content-Type: application/json" -d '{\"query\":\"mutation { createUser(input: {email: \\\"ana@acme.test\\\", name: \\\"Ana\\\"}) { id } }\"}' http://acme.localhost:8080/graphql
```
- The tenant comes from the address. A session for another tenant gets `403 session_tenant_mismatch`; no session gets `401`.
- `/graphql` is answered by `mgr`, which runs in **every cell** and serves only that cell's tenants. Ship a new `mgr` on its own with `.\make deploy-mgr`: it rebuilds `mgr` with a new tag (`<version>-<commit>`) and upgrades only `mgr` in every cell. The tag is recorded in `deploy/versions/local.yaml`, so cells deployed later get the same version.
- If `curl` can't resolve `acme.localhost`, add `--resolve acme.localhost:8080:127.0.0.1`.

Stop everything with `.\make down`. To also delete all data, run `.\make clean`.

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
| `CELLCTL_CHARTS_DIR` | `--charts-dir` | `charts` (the Helm charts `cells deploy` installs) |
| `CELLCTL_ACTOR` | `--actor` | your OS user name; recorded as who created or changed cells and tenants |

**Who did what:** cells and tenants record `created_by` and `updated_by` as `admin:<your-name>`. They appear in the JSON and in a **CREATED BY** column in `cells list` and `tenants list`. Seeded cells show `config`. Records from before this feature show `unknown`. Locally the name part is self-reported, because everyone shares the admin token. On AWS (stage 6) it becomes the verified IAM identity of the caller.

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
  cellctl cells drain <cell-id>
  cellctl cells consolidate <cell-id> [--delete-data]

TENANT LIFECYCLE
  cellctl tenants create <tenant-id> [--name NAME] [--tier standard|premium]
  cellctl tenants delete <tenant-id>
  cellctl tenants list
  cellctl tenants get <tenant-id>
  cellctl tenants move <tenant-id> [--to CELL]
  cellctl tenants promote <tenant-id> [--cell CELL] [--size S]
  cellctl tenants demote <tenant-id> [--delete-data]

MOVES AND AUDIT LOG
  cellctl moves list [--tenant T]
  cellctl moves show <move-id>
  cellctl moves rollback <move-id>
  cellctl moves complete <move-id>
  cellctl audit [--tenant T] [--cell C] [--limit N] [--cursor C]

OVERVIEW
  cellctl status
```

Every command has `--help`, e.g. `cellctl cells deploy --help`.

### Cell lifecycle: deploy → manage → decommission

**1. Deploy a new cell** (after `make up`):
```powershell
cellctl cells deploy cell-4 --capacity 10
```
In one step this:
1. creates the cell's **own k3d cluster**, `cellarch-cell-4`, on the private network;
2. loads the gateway image and installs the `cell` chart with a generated credential, then waits until the gateway is ready;
3. adds `cell-4.cellarch.internal` to the platform cluster's DNS;
4. registers the cell with the control plane.

No restart and no hand-edited files are needed. The control plane checks the new cell before accepting it: it must be healthy, report the right cell ID, and accept the credential. If any step fails, the new cluster is deleted again and nothing is registered. Each extra cell needs roughly 0.5–1 GB of Docker memory.

| Option | Meaning | Default |
|---|---|---|
| `cell-id` | Unique name: 2–63 lowercase letters, digits or hyphens. | — |
| `--capacity` | Maximum tenants on the cell. Fill-first places tenants up to 75% of it, then uses the remaining room. | Control plane's `DEFAULT_CELL_CAPACITY` (4 locally) |
| `--type` | `shared` for many tenants; `dedicated` is reserved for one tenant and never used for pooled placement. | `shared` |
| `--size` | `small`, `medium` or `large`. A label for now; maps to AWS sizes in stage 6. | `small` |
| `--wave` | Release wave for cell-by-cell rollouts (stage 7); `0` is the canary. | `1` |
| `--image` | Gateway image loaded into the new cluster. | `cellarch/cell-gateway:dev` (built by `make up` / `make build`) |

The cell's credential is stored only as a Kubernetes secret in its own cluster, and in the control plane.

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
cellctl cells decommission cell-4                 # deregister and stop its cluster; data is kept
cellctl cells decommission cell-4 --delete-data   # deregister and delete its cluster with its data
```
Decommissioning is refused while the cell still hosts tenants (`cell_not_empty`). Moving tenants off a cell arrives in stage 3. The seed cells from `CELLS` work the same way. A stopped cell cluster comes back with `make up` (re-register it with `cells register`).

**Registering a cell deployed some other way** (advanced): for a cell you run yourself, register it with its credential, which is the cell's `CELL_INTERNAL_TOKEN`. The cell's `url` is the address the router and control plane use, e.g. `http://cell-5.cellarch.internal:8000`, not `localhost`.
```powershell
cellctl cells register cell-5 http://cell-5.cellarch.internal:8000 --credential <the-cell's-token> --capacity 10
```

### Tenant lifecycle

```powershell
cellctl tenants create acme --name "Acme Corp" --tier premium   # placed on a cell automatically
cellctl tenants create globex                                   # name defaults to the ID
cellctl tenants list
cellctl tenants get acme
cellctl tenants delete globex                                   # stops its services, deletes its data
```

```text
TENANT  NAME       TIER      CELL    STATUS  CREATED BY
acme    Acme Corp  premium   cell-1  active  admin:mrinal
globex  globex     standard  cell-1  active  admin:mrinal
```

Tenant IDs are 2–63 characters: lowercase letters, digits and hyphens, starting with a letter or digit (for example `acme`, `acme-eu`, `t-42`). `--tier` defaults to `standard`.

**Each tenant gets its own services and database.** Tenants share cells up to each cell's capacity, but `cellctl tenants create` gives every tenant:
- its own `user-account` and `order-processing` containers in its cell's cluster (Helm release `tenant-<id>`, chart `charts/tenant-stack`), sized by tier;
- its own database on the cell's Postgres server, with a login per service that reaches only that service's schema.

Onboarding is a handshake, and any failure undoes every step:
1. the control plane places the tenant (`provisioning`) and its cell creates the database, logins and the stack's Kubernetes Secret;
2. `cellctl` installs the tenant's stack and waits for it;
3. the tenant becomes `active`.

`cellctl tenants delete` uninstalls the stack and drops the tenant's database, logins and secret.

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
- **Shared limiter (optional):** set `RATE_LIMIT_BACKEND=redis` in `.env`, then `. .\make up`; Redis then runs in the platform cluster. All routers then share **one** bucket per tenant in Redis, so the full limit is exact and `ROUTER_REPLICAS` no longer splits it. It **fails open**: if Redis is down, each router falls back to its own limits, and Redis is retried every few seconds. Traffic never stops because of Redis.

### Moving a tenant to another cell

```powershell
cellctl tenants move acme --to cell-2    # or leave out --to and placement chooses a cell
cellctl moves list --tenant acme
```

```text
Moving acme from cell-1 to cell-2 (mv_3f2a9c1b04de); writes are paused.
Starting acme's services in cell-2's cluster...
Copying acme's data to cell-2...
Copied 42 rows. Switching acme to cell-2...
acme is served by cell-2. Removing it from cell-1...
Tenant acme moved to cell-2.
```

The tenant keeps its users, orders and IDs; cursors and `Idempotency-Key`s from before the move still work. What happens:
1. **Start:** the tenant becomes `migrating` and is frozen on its cell. Reads keep working; writes get `503 tenant_migrating` with `Retry-After`. The target cell creates the tenant's database.
2. `cellctl` installs the tenant's stack on the target. Its services create their tables.
3. **Copy:** the control plane streams the old cell's export into the new cell. It refuses a target with data or another schema version.
4. **Switch:** the route points to the new cell and the tenant is `active` again.
5. `cellctl` uninstalls the old stack, and the old cell drops its copy of the tenant.

- **If a step before the switch fails,** the move is rolled back: the tenant stays on its old cell with all its data and takes writes again.
- **If `cellctl` is interrupted,** `cellctl moves rollback <move-id>` finishes the rollback.
- **If the old cell can't drop its copy,** the move still completes, flagged `cleanup_pending`. Retry with `cellctl moves complete <move-id>`.

See [docs/plan.md §5](docs/plan.md).

### Dedicated cells, draining and consolidating

All four run the move above, one tenant at a time.

```powershell
cellctl tenants promote acme              # deploys cell-acme (dedicated, capacity 1) and moves acme there
cellctl tenants demote acme               # back to a shared cell; cell-acme is stopped (--delete-data removes it)
cellctl cells drain cell-1                # no new tenants on cell-1; its tenants move to other cells
cellctl cells consolidate cell-3          # drain cell-3, then decommission it
```

- **A dedicated cell holds exactly one tenant.** It always has capacity 1, and new tenants are never placed on it. A tenant on one shows `placement_mode: dedicated`; after a demote it shows `pooled` again.
- **A failed promote removes the new cell again,** and the tenant stays where it was.
- **Drain and consolidate check for room first.** If the other cells can't take every tenant, they refuse before moving anything.
- **If one move fails during a drain,** that tenant is rolled back and the drain stops. Run the drain again to move the rest.

See [docs/plan.md §5.1](docs/plan.md).

### Audit log

Every admin change is recorded: cells, tenants, rate limits and every move step, including refused and failed ones.

```powershell
cellctl audit --tenant acme
```

```text
TIME                              ACTOR         ACTION         OUTCOME    TENANT  CELL           ERROR
2026-11-14T10:02:11.031829+00:00  admin:mrinal  move.complete  succeeded  acme    cell-1->cell-2  -
2026-11-14T10:02:10.912002+00:00  admin:mrinal  move.switch    succeeded  acme    cell-1->cell-2  -
```

Newest first. `--cell`, `--limit` and `--cursor` page through the rest. Raw form: `GET /v1/audit`.

Seasonal schedules (stage 5) will be added here.

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

### Usage per tenant

```powershell
cellctl usage
```
```text
Usage since 2026-11-12T10:00:00+00:00 (this router; 1 replica(s))

TENANT  REQUESTS  THROTTLED  2XX  4XX  5XX
acme    120       30 (25%)   85   34   1
globex  4         0 (0%)     4    0    0
```
- **What's counted:** the router counts each tenant's requests, how many were throttled, and the responses by class, busiest tenant first. Counters reset when the router restarts.
- **What to do with it:** a tenant that's throttled a lot is a candidate for a higher limit or, from stage 3, a dedicated cell.
- **Access:** `cellctl usage` reads the router's stats with the **metrics token**, not the admin token. It takes `ROUTER_METRICS_TOKEN` from `.env`, or `--metrics-token` / `CELLCTL_METRICS_TOKEN`. If your `.env` predates this feature, add `ROUTER_METRICS_TOKEN=dev-metrics-token` (see `.env.example`).
- **Raw JSON:** available at `http://localhost:8080/_router/stats` with header `X-Internal-Token: <metrics token>`.

### Cell overload protection

Each cell handles at most `MAX_CONCURRENT_REQUESTS` requests at once (default 100). Beyond that, it rejects new requests **immediately** with `503 cell_overloaded` and `Retry-After: 1`, instead of queueing them. That way a spike can't pile up and take the cell down. Health checks are exempt. This backs up the router's per-tenant rate limits.

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
| `rate_limited` | (tenant API, `429`) The tenant is over its rate limit; retry after `Retry-After` seconds. |
| `cell_overloaded` | (tenant API, `503`) The tenant's cell is at its request cap; retry shortly. |

- **Output:** lists print as tables; single records as JSON; progress messages go to stderr.
- **Exit codes:**
  - `0` on success;
  - `1` when the control plane or Docker returns an error, or can't be reached;
  - `2` for usage errors, such as a missing token, an invalid option or a missing chart.

## Images and Helm charts

Each component has **its own image**, built from one multi-stage Dockerfile. Each image contains only that component's code:
```powershell
docker build --target router -t cellarch/router:dev .   # also: control-plane, cell-gateway, user-account, order-processing, dev-idp
docker build --target mgr -t cellarch/mgr:0.1.0-abc1234 .   # mgr: its own version, released on its own
```

Helm charts for the Kubernetes runtime (stage 2.6, one cluster per cell):

| Chart | Installed into | Contains |
|---|---|---|
| `charts/platform` | the platform cluster | router, control plane, optional Redis, and locally the edge proxy (the production ALB's stand-in: every tenant host on one port) |
| `charts/dev-idp` | the platform cluster, **development only** | the identity provider stand-in for the web UI's sign-in |
| `charts/cell` | **each cell's own cluster** | the cell gateway and the cell's database server |
| `charts/tenant-stack` | each cell's cluster, one release per tenant | the tenant's `user-account` and `order-processing` |
| `charts/mgr` | **each cell's own cluster**, release `mgr` | the web UI's GraphQL backend, with its own autoscaler, disruption budget and network policy |

- **Hardened by default:** non-root, read-only filesystem, all capabilities dropped, probes and resource limits, tokens only from Kubernetes secrets.
- **Network policies:** deny all ingress, then allow only what each component needs.

The charts are tested with `pytest tests/charts` (needs `helm`), and `make up` installs them into the local k3d clusters.

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
  cli/                     cellctl and the local cluster toolkit (k3d, Helm, cell DNS)
  services/                per-tenant microservices (stage 2.5; wired into cells next)
    common/                migrations, idempotency keys, cursor pagination, auth, health probes
    user_account/          a tenant's users
    order_processing/      a tenant's orders (checks users via its own user-account)
tests/acceptance/          stage acceptance tests against the in-process platform
tests/unit/                unit tests per component
scripts/demo.py            end-to-end walkthrough (also the k3d smoke test in CI)
scripts/dev.py             local tasks behind make.ps1 (Windows) and Makefile (macOS/Linux)
scripts/ci/pr_gate.py      PR policy gate
charts/platform/           Helm chart: router + control plane (platform cluster)
charts/cell/               Helm chart: one cell's gateway (the cell's own cluster)
tests/charts/              chart tests (helm template + lint)
Dockerfile                 one image per component (multi-stage targets)
```

## License

MIT
