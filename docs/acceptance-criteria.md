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
| A22 | the local Docker stack | `cellctl cells deploy <cell-id>` is run | a container is started with a generated credential, registered, and ready for tenants in one step |
| A23 | a deployed container that never becomes healthy | `cellctl cells deploy` runs | the container and its generated file are removed and nothing is registered (exit code `1`) |
| A24 | a cell deployed with `cellctl` and no tenants | `cellctl cells decommission <cell-id> --delete-data` is run | it is deregistered, its container stopped and its data volume removed |

### Who created and changed what

| ID | Given | When | Then |
|---|---|---|---|
| A25 | admin calls that name the caller (`X-Actor`, sent by `cellctl`) | a cell or tenant is created, then changed | `created_by` / `updated_by` record `admin:<name>` (or `admin` without a name) and `updated_at` moves on |
| A26 | the admin API | the caller name has spaces, symbols, non-ASCII characters or is over 64 characters | the request is rejected with `400 invalid_actor` |

Stage 1 also has a demo acceptance check: `scripts/demo.py` runs end-to-end against the in-process platform (`tests/acceptance/test_demo.py`) and against the real compose stack in CI (`compose-smoke`).
