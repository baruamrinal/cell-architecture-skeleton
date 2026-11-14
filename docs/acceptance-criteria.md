# Acceptance criteria

What each delivery stage must do, in plain language. The precise, executable version is the acceptance tests in [tests/acceptance/](../tests/acceptance/). Each criterion ID matches a test named `test_<id>_…` (for example, A7 → `test_a7_cell_rejects_a_tenant_it_does_not_host`).

**Keeping them in sync:** `tests/acceptance/test_traceability.py` fails CI if a criterion here has no test, or a test has no criterion here. Add a new behavior in both places, in the same PR. IDs are never reused; new stages continue the numbering.

**Proof:** CI runs every acceptance test on every PR (`lint-type-test` job).

## Stage 1: core platform

| ID | Given | When | Then |
|---|---|---|---|
| A1 | an active shared cell | a tenant is onboarded | it is placed on that cell, set up there, `active` and `pooled` |
| A2 | an onboarded tenant | it calls the API through the router with `X-Tenant-ID` | the request reaches its cell (`X-Cell-ID` matches) and its data is returned |
| A3 | two shared cells with capacity 4 | four tenants are onboarded | the first cell is filled to the 75% target (3 tenants) before the second is used |
| A3b | every shared cell is at the 75% target | another tenant is onboarded | it goes into the remaining room and a "capacity low" warning is logged |
| A4 | cells that are draining, disabled or dedicated | a pooled tenant is onboarded | those cells are never chosen (`503` if nothing else is eligible) |
| A5 | every eligible cell is completely full | a tenant is onboarded | the API returns `503 no_capacity` |
| A6 | two tenants on the same shared cell | one tenant lists, reads or deletes the other's item | it sees nothing (`[]` / `404`); the owner still has the item |
| A7 | a tenant hosted on cell-1 | a request for it reaches cell-2 | cell-2 rejects it with `421 tenant_not_in_cell` |
| A8 | the router | a request has no tenant header, a malformed one, or an unknown tenant | it gets `400 missing_tenant`, `400 invalid_tenant` or `404 unknown_tenant` |
| A9 | the router | a client requests `/internal/*` (including dotted or encoded paths) or sends `X-Internal-Token` | the request gets `404` and never reaches a cell's `/internal`; the token header is stripped before forwarding |
| A10 | the control plane is down | a tenant the router has seen before sends a request | it keeps working from the last-known route; a never-seen tenant gets `503 routing_unavailable` |
| A11 | the router has a stale cached route | the old cell answers `421` | the router refreshes the route and retries once on the new cell |
| A12 | tenants on cell-1 and cell-2 | cell-1 becomes unreachable | cell-1's tenants get `502 cell_unreachable`; cell-2's tenants are unaffected |
| A13 | a cell's `/internal` API | it is called with no token, another cell's token or the admin token | it is rejected (`401`); only its own credential works. Cells can't be registered without a configured credential (`422`) |
| A14 | the control plane | admin and routing APIs are called with missing, wrong or the router credential | missing/wrong → `401`; router credential on admin APIs → `403`; routing API accepts router and admin credentials |
| A15 | the chosen cell can't set up the tenant | a tenant is onboarded | the API returns `502 provisioning_failed` and no tenant record is left behind |

### Cell lifecycle commands (stage 1 extension)

| ID | Given | When | Then |
|---|---|---|---|
| A16 | a running cell the control plane has no credential for | it is registered through the API with its credential | it is accepted without restarting the control plane, and tenants can be placed on and served by it |
| A17 | a cell being registered | it is unreachable, reports a different cell ID, or rejects the credential | registration fails with `422 cell_unreachable`, `cell_id_mismatch` or `cell_credential_rejected`, and nothing is registered |
| A18 | a cell registered with a credential | cells, a cell or the status are read, or registration fails validation | the credential never appears in any response, including validation errors |
| A19 | a cell hosting 2 tenants | its capacity is set below 2, or raised | lowering below the tenant count fails with `409 capacity_below_tenants`; raising succeeds |
| A20 | one cell with tenants and one without | each is decommissioned | the cell with tenants is refused (`409 cell_not_empty`); the empty cell and its credential are removed |
| A21 | cells filling up | the platform status is read | it shows totals (cells, placeable cells, tenants, capacity, free slots) and warns `capacity_low` past the 75% target and `no_capacity` when full |
| A22 | the local k3d platform | `cellctl cells deploy <cell-id>` is run | a new cell **cluster** is created, the cell chart is installed with a generated credential, the cell is reachable by name (`<cell-id>.cellarch.internal`), registered, and ready for tenants in one step |
| A23 | a new cell whose gateway never answers | `cellctl cells deploy` runs | its cluster and DNS name are removed again and nothing is registered (exit code `1`) |
| A24 | a cell deployed with `cellctl` and no tenants | `cellctl cells decommission <cell-id> --delete-data` is run | it is deregistered and its cluster deleted with its data (without `--delete-data` the cluster is only stopped) |

### Who created and changed what

| ID | Given | When | Then |
|---|---|---|---|
| A25 | admin calls that name the caller (`X-Actor`, sent by `cellctl`) | a cell or tenant is created, then changed | `created_by` / `updated_by` record `admin:<name>` (or `admin` without a name) and `updated_at` moves on |
| A26 | the admin API | the caller name has spaces, symbols, non-ASCII characters or is over 64 characters | the request is rejected with `400 invalid_actor` |

## Stage 2: traffic protection

### Rate limits

| ID | Given | When | Then |
|---|---|---|---|
| A31 | a tenant that has used up its limit | it sends another request | the router answers `429 rate_limited` with `Retry-After`, and the request never reaches the cell |
| A32 | two tenants on the same cell, one throttled | the other sends requests | the other tenant is unaffected |
| A33 | a tenant with a burst of 2 at 1 request/s | it sends 3 requests at once, then waits a second | 2 succeed, the 3rd is throttled, and after the wait it can send again |
| A34 | tenants on each tier | limits are read, overridden and cleared | the tier default applies (standard 50/s burst 100, premium 500/s burst 1000); an override replaces it; clearing restores it; the router applies changes after its route cache refreshes |
| A35 | a tenant the router has seen | the control plane goes down | its limit is still enforced from the cached route |
| A36 | a router configured as one of 2 router replicas | a tenant with a burst of 4 sends requests | that router allows half (2), so all routers together stay within the limit |
| A37 | any request the router forwards | the response comes back | it carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` |

### Usage metering and cell request cap

| ID | Given | When | Then |
|---|---|---|---|
| A38 | tenants sending requests through the router, some throttled | the router's usage stats are read | each known tenant's requests, throttled requests and responses by class (2xx/4xx/5xx) are counted; unknown tenant IDs are not tracked; reading stats needs the router's metrics token |
| A39 | a cell already handling its maximum number of requests at once | another request arrives | it is rejected immediately with `503 cell_overloaded` and `Retry-After`, while `/healthz` still answers |

### Shared rate limiter (Redis)

| ID | Given | When | Then |
|---|---|---|---|
| A40 | a router with the Redis limiter switched on, but Redis down | a tenant sends requests | requests are still limited by the router's own in-memory limiter (fail-open: traffic never stops because Redis is down) |
| A41 | two routers sharing one Redis | a tenant with a burst of 4 sends requests through both | the routers share one bucket: 4 requests in total succeed, the next is throttled (runs when `REDIS_URL` points at a Redis; skipped otherwise) |

## Stage 2.5: tenant services

### A tenant's service stack (user-account + order-processing)

| ID | Given | When | Then |
|---|---|---|---|
| A42 | a tenant's user-account and order-processing services | a user is created, then an order for that user | the order is `placed` with the computed total, can be read back, and cancelled once (`cancelled`; cancelling again is `409`) |
| A43 | the same stack | an order is placed for a user that doesn't exist | it is rejected with `422 unknown_user` and nothing is stored |
| A44 | the tenant's user-account is down | an order is placed | order-processing answers `503 dependency_unavailable` with `Retry-After`, and stays ready for reads |
| A45 | a create request sent with an `Idempotency-Key` | it is retried with the same key and body, or with the same key and a different body | the retry returns the original response without creating a duplicate (`Idempotent-Replay: true`); a different body is rejected with `422 idempotency_key_reused` |
| A46 | more users or orders than one page | they are listed with a `limit` | results come in creation order with a `next_cursor`, and following the cursors returns every record exactly once |
| A47 | a service of one tenant | it is called without the stack's credential, or for another tenant | it is rejected (`401`, or `421 tenant_mismatch`) |
| A48 | a service's database | the service starts, or `/healthz` and `/readyz` are called | versioned migrations are applied once and recorded; `/healthz` answers while the process is alive, `/readyz` only while its database works |
| A49 | several instances of a service starting at the same time against one database | each applies migrations | every migration is applied exactly once and every instance starts |

A42–A49 run on the SQLite stand-in and, when `POSTGRES_URL` points at a Postgres server, on PostgreSQL (the production engine).

Stage 1 also has a demo acceptance check: `scripts/demo.py` runs end-to-end against the in-process platform (`tests/acceptance/test_demo.py`) and against the real compose stack in CI (`compose-smoke`).

### A tenant's own stack and database in its cell

Every tenant gets its own `user-account` and `order-processing` containers and its own database on the cell's database server. This replaces the sample `items` API and the pooled/isolated tier (A27–A30, retired).

| ID | Given | When | Then |
|---|---|---|---|
| A50 | a cell with room | a tenant is onboarded (`cellctl tenants create`) | the control plane places it (`provisioning`); the cell creates the tenant's database, a login per service and the stack's secret; the tenant's stack is installed in the cell's cluster; the tenant becomes `active` and its users and orders are served through the router |
| A51 | a tenant whose stack is not running yet | it is activated, or it sends requests | activation fails with `409 tenant_stack_not_ready` and the tenant stays `provisioning`; its requests get `503 tenant_unavailable` from the router (and `503 tenant_not_ready` from the cell itself) |
| A52 | two tenants on the same cell | each creates users and orders through the router | each is served by its own stack and database: neither can list, read or order for the other's users |
| A53 | two tenants on the same cell, one tenant's stack down | both send requests | only that tenant gets `503 tenant_stack_unavailable` (with `Retry-After`); the other is unaffected |
| A54 | an onboarded tenant with data | it is deleted (`cellctl tenants delete`) | its stack is uninstalled, its database, logins and secret are dropped, and it is gone from the control plane and the cell (the router answers `404 unknown_tenant`) |
| A55 | onboarding where any step fails (no capacity, the cell refuses, the stack doesn't start) | `cellctl tenants create` runs | every step done so far is undone: no tenant record, database or stack is left behind |
| A56 | a tenant's database on the cell's Postgres server | each service logs in with its own login | it can use only its own schema in its own tenant's database: not the other service's schema, and not another tenant's database (runs when `POSTGRES_URL` is set) |

## Stage 3: tenant moves

### Moving a tenant between cells

One procedure moves a tenant from one cell to another (docs/plan.md §5): promote, demote, drain and consolidate reuse it.

| ID | Given | When | Then |
|---|---|---|---|
| A57 | a tenant with users, orders and an idempotent create on cell-1 | it is moved to cell-2 (`cellctl tenants move acme --to cell-2`) | it is served from cell-2 (`X-Cell-ID`) with the same users, orders and IDs; cursors from before the move still work; retrying the create with the same `Idempotency-Key` replays the original answer; new users and orders get fresh keys; its stack, database and secret are gone from cell-1 and the move is `completed` |
| A58 | a tenant being moved (`migrating`) | it sends reads and writes, through a fresh route and through a router whose cached route is still `active` | reads are served; writes get `503 tenant_migrating` with `Retry-After`, from the router or from the frozen source cell |
| A59 | a move that fails before the switch (the target stack never starts, the target refuses the copy, or the target stack isn't ready to take over) | `cellctl tenants move` runs | it is rolled back: the tenant is `active` on cell-1 with all its data and takes writes again, nothing of it is left on cell-2, and the move is `rolled_back` |
| A60 | a moved tenant whose source cell fails to delete the old copy | the move completes | the tenant is served from the target; the move is `completed` with `cleanup_pending` set, so the leftover can be cleaned up |
| A61 | a router that cached the tenant's old route | the tenant has been moved | the old cell answers `421`; the router refreshes the route and the request is served by the new cell |
| A62 | a move to the same cell, an unknown, inactive or full cell, of a tenant that isn't `active`, or of a tenant already being moved | the move is requested | it is refused (`409` / `404` / `422` with the reason) and nothing changes |
| A63 | a moved tenant on a cell that hosts another tenant | each lists and reads users and orders | neither sees the other's data: the import wrote only into the moved tenant's own database |
| A64 | a tenant's data on a cell's Postgres server | it is exported and imported into another server | every row, key and idempotency record arrives, the target's sequences continue after the highest key, and the import refuses a target with data or another schema version (runs when `POSTGRES_URL` is set) |

### Audit log

| ID | Given | When | Then |
|---|---|---|---|
| A65 | admin changes: cells registered and changed, tenants created and deleted, rate limits set, every move step | they succeed, are refused or fail | each is in `GET /v1/audit` with who, what, the tenant and cells, the outcome and the time, newest first, with cursor pagination and filters by tenant and cell; `cellctl audit` prints it |
