# cellctl user handbook

`cellctl` is the platform operator's command-line tool. It deploys and retires cells, onboards and moves tenants, and reads the move history and audit log. It talks only to the control plane's admin API, which is never on the request path, so tenants keep being served while you use it.

- **Who uses it:** platform operators who hold the admin credential. Tenants never use it; they use the tenant web UI.
- **Examples:** the example output below comes from a local stack. Tenant names, ids and times are examples.

## Contents

- [Getting started](#getting-started)
- [Command reference](#command-reference)
- [Cells](#cells)
- [Tenants](#tenants)
- [Moves, audit, status and usage](#moves-audit-status-and-usage)
- [Common workflows](#common-workflows)
- [Errors and troubleshooting](#errors-and-troubleshooting)

## Getting started

`cellctl` is installed into the project's virtual environment. Open a terminal in the project folder and start the local stack, which also activates the environment:

```powershell
. .\make up        # Windows
make up            # macOS / Linux
cellctl status
```

If `cellctl` isn't found, the virtual environment isn't active in that terminal. Run `.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate` (macOS/Linux), or use `python -m cellarch.cli.cellctl`.

Locally, each cell is its own k3d cluster, and `cellctl` drives `k3d` and `helm` directly, so both must be on your `PATH`. On AWS, later stages sign requests with IAM and hand cell deploys to the cell-lifecycle workflow.

### Configuration

Each setting can be given in four ways. The first match wins:

1. a command-line option, such as `--token`;
2. an environment variable, such as `CELLCTL_TOKEN`;
3. the same `CELLCTL_*` setting in the project's `.env` file;
4. for the token only, `ADMIN_TOKEN` in `.env`. A project set up with `make up` needs no extra configuration.

| Option | Environment variable | Default | What it is |
| --- | --- | --- | --- |
| `--url` | `CELLCTL_URL` | `http://localhost:8000` | Control plane address |
| `--token` | `CELLCTL_TOKEN` | `ADMIN_TOKEN` from `.env` | Admin credential (required) |
| `--actor` | `CELLCTL_ACTOR` | Your OS user name | Name recorded as `created_by` / `updated_by` and in the audit log |
| `--charts-dir` | `CELLCTL_CHARTS_DIR` | `charts` | Helm charts used to deploy cells and tenant services |
| `--router-url` | `CELLCTL_ROUTER_URL` | `http://localhost:8080` | Router address, used only by `usage` |
| `--metrics-token` | `CELLCTL_METRICS_TOKEN` | `ROUTER_METRICS_TOKEN` from `.env` | Router metrics token, used only by `usage` |

- **Option order:** global options go before the command. Write `cellctl --actor alice tenants list`, not `cellctl tenants list --actor alice`.
- **The admin token:** don't pass it on the command line in a shared terminal or a script, because it ends up in shell history. Keep it in `.env`, which git ignores, or in an environment variable.

### Output

- **Lists:** print as tables.
- **Single records:** print as JSON.
- **Progress messages:** go to stderr, so you can pipe the result on its own, for example `cellctl tenants get acme > acme.json`.

## Command reference

`cellctl` has 24 commands in six groups. Every command has `--help`, for example `cellctl cells deploy --help`.

| Group | Command | What it does |
| --- | --- | --- |
| `cells` | [`list`](#cells-list) | List every cell with its type, status, tenants and utilization |
| `cells` | [`get`](#cells-get) | Show one cell in full |
| `cells` | [`deploy`](#cells-deploy) | Create a new cell cluster and register it |
| `cells` | [`register`](#cells-register) | Register a cell that was deployed some other way |
| `cells` | [`set-status`](#cells-set-status) | Allow or stop placing new tenants on a cell |
| `cells` | [`set-capacity`](#cells-set-capacity) | Change how many tenants a cell holds |
| `cells` | [`drain`](#cells-drain) | Stop new placements and move every tenant out |
| `cells` | [`consolidate`](#cells-consolidate) | Drain a shared cell, then decommission it |
| `cells` | [`decommission`](#cells-decommission) | Deregister an empty cell and stop or delete its cluster |
| `tenants` | [`list`](#tenants-list) | List every tenant with its tier, cell and status |
| `tenants` | [`get`](#tenants-get) | Show one tenant in full |
| `tenants` | [`create`](#tenants-create) | Onboard a tenant: place it on a cell and start its services |
| `tenants` | [`delete`](#tenants-delete) | Stop a tenant's services and delete its data |
| `tenants` | [`move`](#tenants-move) | Move a tenant and its data to another cell |
| `tenants` | [`promote`](#tenants-promote) | Give a tenant its own dedicated cell and move it there |
| `tenants` | [`demote`](#tenants-demote) | Move a tenant back to a shared cell and remove its dedicated cell |
| `tenants` | [`set-limit`](#tenants-set-limit) | Override a tenant's rate limit, or clear the override |
| `moves` | [`list`](#moves-list) | List moves, newest first |
| `moves` | [`show`](#moves-show) | Show one move in full |
| `moves` | [`rollback`](#moves-rollback) | Undo a move that stopped before its switch |
| `moves` | [`complete`](#moves-complete) | Retry the clean-up of the old cell after a move |
| `audit` | [(none)](#audit) | Who changed what, newest first |
| `status` | [(none)](#status) | Platform capacity overview and warnings |
| `usage` | [(none)](#usage) | Requests per tenant, read from the router |

## Cells

A cell is a complete, isolated copy of the data plane: its own Kubernetes cluster, database server, gateway and tenant services. Cells share nothing, so a failing cell affects only its own tenants. All cells come from the same chart and differ only in their inputs: type, size, capacity and wave.

Cell ids use 2–63 lowercase letters, digits and hyphens, for example `cell-4`.

### cells list

Lists every cell with its type, status, tenant count, capacity and utilization.

```text
cellctl cells list
```

```text
CELL       TYPE       STATUS  TENANTS  CAPACITY  UTILIZATION  CREATED BY   URL
cell-1     shared     active  3        4         75%          config       http://cell-1.cellarch.internal:8000
cell-2     shared     active  1        4         25%          admin:alice  http://cell-2.cellarch.internal:8000
cell-acme  dedicated  active  1        1         100%         admin:alice  http://cell-acme.cellarch.internal:8000
```

Cells seeded from configuration show `config` as their creator.

### cells get

Shows one cell in full, as JSON.

```text
cellctl cells get cell-2
```

```json
{
  "cell_id": "cell-2",
  "url": "http://cell-2.cellarch.internal:8000",
  "type": "shared",
  "hosting": "vendor",
  "size": "small",
  "status": "active",
  "capacity": 4,
  "wave": 1,
  "tenant_count": 1,
  "incoming_tenants": 0,
  "utilization": 0.25,
  "created_at": "2026-11-12T20:21:08+00:00",
  "created_by": "admin:alice",
  "updated_at": "2026-11-12T20:21:08+00:00",
  "updated_by": "admin:alice"
}
```

### cells deploy

Creates a new cell, in this order:

1. its own cluster;
2. the cell chart, with the tenant service images;
3. `mgr`, the web UI's backend;
4. registration with the control plane, under a freshly generated credential.

If any step fails, the new cluster is removed again.

```text
cellctl cells deploy <cell-id> [--type shared|dedicated] [--size small|medium|large]
                     [--capacity N] [--wave N] [--image IMAGE]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--type` | `shared` | `shared` hosts many tenants; `dedicated` hosts one |
| `--size` | `small` | Size of the cell's resources |
| `--capacity` | 100 (control plane setting `DEFAULT_CELL_CAPACITY`); always 1 for a dedicated cell | How many tenants the cell may hold |
| `--wave` | `1` | Rollout wave: changes reach wave 1 cells first |
| `--image` | `cellarch/cell-gateway:dev` | Gateway image to run |

Example: add a fourth shared cell with room for 10 tenants.

```text
cellctl cells deploy cell-4 --capacity 10
```

```text
Creating cluster cellarch-cell-4...
Installing the cell chart and waiting until the gateway is ready...
Starting mgr 0.1.0-263eccd in cell-4...
Registering cell-4 with the control plane...
Cell cell-4 is deployed in its own cluster and ready for tenants.
```

### cells register

Registers a cell that was deployed some other way, for example by a pipeline or in a customer's account. You give its address and its internal credential. The control plane checks that the cell answers, reports the same id and accepts the credential before it registers it.

```text
cellctl cells register <cell-id> <url> [--credential TOKEN] [--type ...] [--size ...]
                       [--capacity N] [--wave N]
```

Example:

```text
cellctl cells register cell-5 http://cell-5:8000 --credential "$CELL5_TOKEN"
```

Read the credential from a variable or a secret store, as above, rather than typing it in. If the control plane already has the cell's credential in `CELL_CREDENTIALS`, leave out `--credential`.

### cells set-status

Controls whether new tenants are placed on a cell. Existing tenants keep being served in every status.

| Status | New tenants placed? | Use it when |
| --- | --- | --- |
| `active` | Yes | Normal operation |
| `draining` | No | You plan to empty the cell |
| `disabled` | No | The cell must take no new work at all |

```text
cellctl cells set-status cell-1 draining
```

### cells set-capacity

Changes how many tenants a cell may hold.

- The value must be a positive whole number.
- It can't be lower than the number of tenants the cell hosts or is receiving.
- A dedicated cell's capacity is always 1.

```text
cellctl cells set-capacity cell-4 20
```

### cells drain

Stops placing tenants on a cell, then moves each of its tenants to another active shared cell, one at a time.

- **Checks before it starts:** every tenant on the cell is active, and the other cells have room for all of them.
- **If a move fails:** that move is rolled back and the drain stops. Run the same command again to carry on.

```text
cellctl cells drain cell-1
```

```text
cell-1 is draining: no new tenants; moving 3 out...
Moving globex from cell-1 to cell-2 (mv_3f2a9c1e5b7d); writes are paused.
...
Tenant globex moved to cell-2.
...
cell-1 is empty.
```

### cells consolidate

Drains a **shared** cell, then decommissions it. Use it to retire an under-used cell. For a dedicated cell, demote its tenant instead.

```text
cellctl cells consolidate <cell-id> [--delete-data]
```

### cells decommission

Deregisters an **empty** cell and stops its cluster.

- **Without `--delete-data`:** the cluster is stopped and its data kept.
- **With `--delete-data`:** the cluster and its data are deleted. This can't be undone.
- **A cell that still has tenants:** refused with `cell_not_empty`. Move or drain its tenants first.

```text
cellctl cells decommission cell-4
cellctl cells decommission cell-4 --delete-data
```

## Tenants

A tenant is one customer organization. Each tenant lives in exactly one cell, with its own services and its own database on that cell's database server.

- **Tenant ids:** 2–63 lowercase letters, digits and hyphens, for example `acme`.
- **Web address:** the id is also the tenant's web address, `http://acme.localhost:8080` locally.

### tenants list

```text
cellctl tenants list
```

```text
TENANT  NAME    TIER      CELL       STATUS  CREATED BY
acme    Acme    premium   cell-acme  active  admin:alice
globex  Globex  standard  cell-1     active  admin:alice
hooli   Hooli   standard  cell-2     active  admin:alice
```

### tenants get

Shows one tenant in full, as JSON, including its cell, tier, status and placement mode (`shared` or `dedicated`).

```text
cellctl tenants get acme
```

### tenants create

Onboards a tenant:

1. The control plane places it on a shared, active cell and creates its database.
2. `cellctl` starts the tenant's services in that cell's cluster.
3. `cellctl` waits until the tenant is active.

If any step fails, the onboarding is undone.

Placement is fill-first: it fills the fullest shared cell that stays at or under the target utilization (75%). This keeps the number of running cells, and so the cost, low.

```text
cellctl tenants create <tenant-id> [--name NAME] [--tier standard|premium]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--name` | The tenant id | Display name |
| `--tier` | `standard` | `standard`: 50 requests/s, burst 100. `premium`: 500 requests/s, burst 1,000 |

Example:

```text
cellctl tenants create globex --name "Globex Corporation" --tier premium
```

```text
Tenant globex placed on cell-1; its database is ready.
Starting globex's services in cell-1's cluster...
Tenant globex is active.
```

### tenants delete

Stops the tenant's services and deletes the tenant and all of its data. This can't be undone.

```text
cellctl tenants delete globex
```

```text
Stopping globex's services in cell-1's cluster...
Tenant globex and its data are deleted.
```

### tenants move

Moves a tenant and its data to another cell, using the single move procedure. Without `--to`, placement chooses the target.

1. **Freeze:** the tenant's writes are paused. Reads keep working, and the tenant's web UI shows read-only mode.
2. **Start services:** the tenant's services start in the target cell's cluster.
3. **Copy:** the tenant's data is copied to the target cell.
4. **Switch:** the router sends the tenant's traffic to the target cell, and writes resume.
5. **Clean up:** the old cell's services and its copy of the data are removed.

If any step before the switch fails, the move is rolled back automatically and the tenant stays where it was. The target must be an active cell with room.

```text
cellctl tenants move <tenant-id> [--to CELL]
```

Example:

```text
cellctl tenants move globex --to cell-2
```

```text
Moving globex from cell-1 to cell-2 (mv_3f2a9c1e5b7d); writes are paused.
Starting globex's services in cell-2's cluster...
Copying globex's data to cell-2...
Copied 42 rows. Switching globex to cell-2...
globex is served by cell-2. Removing it from cell-1...
Tenant globex moved to cell-2.
```

If the old cell can't be cleaned up, the move still succeeds. It then prints a `cellctl moves complete <move-id>` command to retry later.

### tenants promote

Gives a tenant a dedicated cell of its own: it deploys a new dedicated cell, then moves the tenant there. If the move fails, the new cell is removed again. The tenant must be active and not already dedicated.

```text
cellctl tenants promote <tenant-id> [--cell CELL] [--size small|medium|large] [--image IMAGE]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--cell` | `cell-<tenant-id>` | Id of the new dedicated cell |
| `--size` | `small` | Size of the new cell |
| `--image` | `cellarch/cell-gateway:dev` | Gateway image to run |

Example:

```text
cellctl tenants promote acme --size medium
```

This deploys `cell-acme`, moves `acme` there, and prints the finished move.

### tenants demote

Moves a tenant from its dedicated cell back to a shared cell, then decommissions the dedicated cell. Without `--delete-data`, the dedicated cell's cluster is stopped and its data kept.

```text
cellctl tenants demote <tenant-id> [--delete-data]
```

Example:

```text
cellctl tenants demote acme
```

### tenants set-limit

Overrides a tenant's rate limit at the router, or clears the override so the tier default applies again. Give both `--rps` and `--burst`, or `--clear` on its own.

```text
cellctl tenants set-limit <tenant-id> --rps N --burst N
cellctl tenants set-limit <tenant-id> --clear
```

Example: let `globex` send 200 requests per second, with bursts of up to 400.

```text
cellctl tenants set-limit globex --rps 200 --burst 400
```

## Moves, audit, status and usage

### moves list

Lists moves, newest first, optionally for one tenant.

```text
cellctl moves list [--tenant TENANT]
```

```text
MOVE             TENANT  FROM    TO         STATE      CLEANUP  UPDATED
mv_cc11d69c3695  acme    cell-1  cell-acme  completed  -        2026-11-14T07:01:31+00:00
mv_ad9720449f59  globex  cell-3  cell-2     completed  -        2026-11-14T06:46:37+00:00
```

A move goes through these states:

| State | Meaning |
| --- | --- |
| `started` | Writes are paused; services are starting in the target cell |
| `copied` | The data is in the target cell; the switch hasn't happened yet |
| `switched` | The target cell serves the tenant; the old cell isn't cleaned up yet |
| `completed` | Done: the old cell holds nothing of the tenant |
| `rolled_back` | Undone: the tenant stays on its original cell |

A value in the CLEANUP column means the old cell still holds a copy of the tenant. Run `moves complete` to retry the clean-up.

### moves show

Shows one move in full, including the number of rows copied and any error.

```text
cellctl moves show mv_cc11d69c3695
```

```json
{
  "move_id": "mv_cc11d69c3695",
  "tenant_id": "acme",
  "source_cell_id": "cell-1",
  "target_cell_id": "cell-acme",
  "state": "completed",
  "cleanup_pending": null,
  "error": null,
  "copied_rows": 5,
  "created_at": "2026-11-14T07:01:13+00:00",
  "created_by": "admin:alice",
  "updated_at": "2026-11-14T07:01:31+00:00",
  "updated_by": "admin:alice"
}
```

### moves rollback

Undoes a move that stopped before its switch, in state `started` or `copied`, for example because `cellctl` was interrupted.

- **What it does:** stops the tenant's services in the target cell and opens writes on the original cell again.
- **After the switch:** a move that has switched can't be rolled back. Move the tenant back instead.

```text
cellctl moves rollback mv_3f2a9c1e5b7d
```

```text
Stopping globex's services in cell-2's cluster...
Move mv_3f2a9c1e5b7d is rolled_back.
```

### moves complete

Retries removing the tenant's old copy from the source cell after a move has switched. Use it when a move, or `moves list`, reports a clean-up pending.

```text
cellctl moves complete mv_3f2a9c1e5b7d
```

### audit

Shows who changed what, newest first: every create, delete, move, status change and rate-limit change, with its outcome and any error. Results come in pages. When there are more, the last line prints the command for the next page.

```text
cellctl audit [--tenant TENANT] [--cell CELL] [--limit N] [--cursor CURSOR]
```

Example: the last three events.

```text
cellctl audit --limit 3
```

```text
TIME                       ACTOR        ACTION           OUTCOME    TENANT  CELL    ERROR
2026-11-14T12:18:40+00:00  admin:alice  tenant.delete    succeeded  hooli   cell-2  -
2026-11-14T12:18:30+00:00  admin:alice  tenant.activate  succeeded  hooli   cell-2  -
2026-11-14T12:18:13+00:00  admin:alice  tenant.create    succeeded  hooli   cell-2  -
More: cellctl audit --cursor eyJhZnRlciI6NDJ9 --limit 3
```

`--limit` defaults to 20.

### status

Gives the platform's capacity at a glance: cells, tenants, free slots, the per-cell table and any warnings. Check it before onboarding many tenants and after any cell change.

```text
cellctl status
```

```text
Target utilization: 75%
Cells: 3 (2 placeable)   Tenants: 5   Capacity: 9   Free slots: 4

CELL       TYPE       STATUS  TENANTS  CAPACITY  UTILIZATION  ...
cell-1     shared     active  3        4         75%
cell-2     shared     active  1        4         25%
cell-acme  dedicated  active  1        1         100%

No warnings.
```

Placeable cells are shared and active; new tenants go only there. Two warnings can appear:

| Warning | Meaning | What to do |
| --- | --- | --- |
| `no_capacity` | No placeable cell has room, so new tenants will be refused | Deploy a cell or raise a capacity |
| `capacity_low` | Every placeable cell is at or past the target utilization | Plan a new cell soon |

### usage

Shows requests per tenant since the router started: the total, the throttled requests and the responses by class.

- **Where it reads from:** the router, not the control plane, so it needs the router metrics token (`ROUTER_METRICS_TOKEN` in `.env` locally).
- **Counts:** they are per router replica.

```text
cellctl usage
```

```text
Usage since 2026-11-14T10:52:40+00:00 (this router; 1 replica(s))

TENANT  REQUESTS  THROTTLED  2XX  4XX  5XX
acme    31        0 (0%)     31   0    0
globex  23        0 (0%)     23   0    0
hooli   15        0 (0%)     11   4    0
```

## Common workflows

### Onboard a new tenant

1. Check there's room: `cellctl status`. If it warns `no_capacity`, add a cell first: `cellctl cells deploy cell-4`.
2. Create the tenant: `cellctl tenants create globex --name "Globex Corporation"`.
3. Confirm it's active and see its cell: `cellctl tenants get globex`.
4. The tenant can now sign in at `http://globex.localhost:8080`.

### Give a large tenant its own cell

1. Promote it: `cellctl tenants promote acme --size medium`. During the move the tenant can read but not write, and its web UI shows read-only mode.
2. Confirm it: `cellctl tenants get acme` shows `cell-acme` and placement mode `dedicated`.
3. If it later shrinks, demote it: `cellctl tenants demote acme`.

### Retire an under-used shared cell

1. Check the other cells have room: `cellctl status`.
2. Consolidate the cell: `cellctl cells consolidate cell-3`. Its tenants move out one at a time. Then the cell is decommissioned and its cluster stopped, with its data kept.
3. If a move fails, the drain stops and that move is rolled back. Fix the cause, then run the same command again to carry on.
4. To delete the cell's data in the same step, use `cellctl cells consolidate cell-3 --delete-data` instead.

### Take a cell out of placement for maintenance

1. Run `cellctl cells set-status cell-2 disabled`. Its tenants keep being served, and no new tenants are placed there.
2. Do the work.
3. Run `cellctl cells set-status cell-2 active`.

### Recover an interrupted move

1. Find the move: `cellctl moves list --tenant globex`.
2. If its state is `started` or `copied`, undo it with `cellctl moves rollback <move-id>`, then run the move again.
3. If its state is `switched` with a clean-up pending, finish it: `cellctl moves complete <move-id>`.

### Find out who changed a tenant

```text
cellctl audit --tenant globex --limit 50
```

## Errors and troubleshooting

Errors print on one line, as `error: <code>: <message> (HTTP <status>)`:

```text
error: move_not_found: move mv_nope not found (HTTP 404)
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | The control plane, a cell, `k3d` or `helm` returned an error or couldn't be reached |
| `2` | Usage error: a missing token, an invalid option or id, or a missing chart |

### Common errors

| Error | Meaning | What to do |
| --- | --- | --- |
| `unauthenticated` (401) | The admin token is missing or wrong | Check `ADMIN_TOKEN` in `.env` or `CELLCTL_TOKEN` |
| `unreachable` | The control plane didn't answer at `--url` | Start the stack (`make up`) or check `CELLCTL_URL` |
| `k3d not found` / `helm not found` | The tool isn't on this terminal's `PATH` | Install it, then open a new terminal |
| `no_capacity` (503) | No placeable cell has room | Deploy a cell or raise a cell's capacity |
| `tenant_exists` / `cell_exists` (409) | That id is already in use | Pick another id |
| `tenant_not_found` / `cell_not_found` / `move_not_found` (404) | No such id | Check it with `tenants list`, `cells list` or `moves list` |
| `tenant_not_active` (409) | The tenant is being set up, moved or deleted | Wait, or check `moves list --tenant <id>` |
| `move_in_progress` (409) | The tenant is already being moved | Wait for that move, or roll it back |
| `same_cell` (422) | The tenant is already on the target cell | Choose another target |
| `cell_not_active` / `cell_full` (409) | The target cell isn't active, or has no room | Choose another target or change the cell |
| `capacity_below_tenants` (409) | The new capacity is below the cell's tenant count | Use a higher capacity, or move tenants off first |
| `cell_not_empty` (409) | A cell with tenants can't be decommissioned | Drain the cell first |
| `cell_not_shared` | `consolidate` was used on a dedicated cell | Demote its tenant instead |
| `tenant_already_dedicated` / `tenant_not_dedicated` | Promote or demote doesn't apply to this tenant | Check `tenants get <id>` |
| `missing_cell_credential` (422) | `register` had no credential, and none is configured | Pass `--credential` |
| `cell_unreachable` / `cell_id_mismatch` / `cell_credential_rejected` (422) | The cell failed the checks done before it's registered | Check the cell's address, id and credential |
| `provisioning_failed` | The chosen cell couldn't set the tenant up | Nothing is left behind; retry once the cell is healthy |
| `validation_error` (422) | An id or option isn't valid | See the command's `--help` |

### Troubleshooting

- **`cellctl` is "not recognized":** the virtual environment isn't active in this terminal. Run `. .\make up`, or activate `.venv`.
- **Activation fails with "running scripts is disabled" (Windows):** run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
- **`k3d not found` although it's installed:** the terminal was opened before the install. Open a new terminal, or reload the `PATH` in this one:

  ```powershell
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
  ```

- **A move stopped half-way:** see [Recover an interrupted move](#recover-an-interrupted-move). The tenant can still read, and a rollback reopens writes.
- **A drain or consolidate stopped:** the failed move was rolled back. Fix the cause and run the same command again; it carries on with the tenants still on the cell.
- **`usage` says to set a metrics token:** add `ROUTER_METRICS_TOKEN` to `.env`, or pass `--metrics-token`.
