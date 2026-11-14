# Plan: Cell-Based Architecture Reference Implementation

A foundational reference implementation of cell-based architecture for a multi-tenant SaaS platform, with automated infrastructure (AWS + Terraform/OpenTofu) and CI/CD (GitHub Actions).

Status: **plan approved for v1, not yet implemented.**

---

## 1. Goals

1. **Isolation**: tenants are partitioned into independent cells. A failure, bad deploy or noisy tenant affects only one cell.
2. **Elastic placement**: tenants start pooled and can be moved to dedicated cells (for a season or permanently) and back, with no client-visible change.
3. **Cost-effective**: few, well-filled cells. Seasonal and standby capacity scales to zero. Costs are attributable per cell and per tenant.
4. **Automated**: every cell is stamped from one Terraform module and rolled out in waves by CI/CD. A new cell is secure and observable by construction.
5. **Hosting-flexible**: a cell can run in our AWS account or in a customer's AWS account.
6. **Enterprise design, skeleton implementation**: the structure is production-grade from day one; only the amount of implementation is minimal (§1.1).

### 1.1 Enterprise design principles

This is a skeleton, but it must meet an enterprise design: it differs from production only in *how much* is implemented, never in *how it is structured*. Every change must satisfy:

1. **A production equivalent exists and is documented** in [production-architecture.md](production-architecture.md). Local stand-ins (SQLite, Docker, tokens in `.env`) sit behind the same interface as their production counterparts (Aurora, EKS, Secrets Manager).
2. **No shortcuts in the boundaries:**
   - control plane vs data plane, cells sharing nothing, per-tenant data and runtime;
   - internal APIs never exposed;
   - one credential per role.
3. **Secure by default:** no secrets in git or logs, non-root and read-only containers, pinned and scanned images, validated input, no echoing of submitted values.
4. **Operable:**
   - structured logs carrying `request_id`, `tenant_id` and `cell_id`;
   - separate liveness (`/healthz`) and readiness (`/readyz`);
   - graceful shutdown;
   - versioned schema migrations;
   - configuration validated at startup and documented.
5. **Stable APIs:** versioned paths (`/v1`), one error shape, cursor pagination for lists, idempotency keys on creates.
6. **Tested like production:** TDD, acceptance criteria per behavior, real-dependency adapter tests (Postgres, Redis, Docker) in CI.

---

## 2. Architecture overview

```
                          ┌──────────────────── Control plane ────────────────────┐
                          │ cell registry · tenant registry · placement           │
                          │ tenant moves · seasonal scheduler · cell provisioner  │
                          │ limits & tiers · audit log                            │
                          └───────────────┬───────────────────────────────────────┘
                                          │ route lookups (cached)   ▲ pushes changes
client ──X-Tenant-ID──► Router ───────────┼──────────────────────────┤
                           │ rate limit, metering, tenant routing     │
                           ├──► cell-1  (shared)     app + own DB ◄───┤
                           ├──► cell-2  (shared)     app + own DB ◄───┤
                           ├──► cell-s-acme (seasonal dedicated)  ◄───┤
                           └──► cell-cust-globex (customer AWS, PrivateLink)
                          └────────────────────── Data plane ──────────────────────┘
```

### 2.1 Control plane vs data plane

| | Control plane | Data plane |
|---|---|---|
| Components | `control_plane/` | `data_plane/router/`, `data_plane/cell/` |
| Job | Manage: register cells, place and move tenants, schedules, limits | Serve: route, rate-limit, run the app, store tenant data |
| On request path | No | Yes |
| If it fails | No onboarding or moves; **traffic continues** | Only tenants of the affected cell are impacted |

**Static stability rule:** the data plane never depends on the control plane being up.
- The router caches routes and falls back to the last-known routes if the control plane is unreachable.
- Each cell keeps its own list of the tenants it hosts.
- The control plane pushes changes to cells; cells never pull from it at request time.

### 2.2 Components

**Control plane**
- Cell registry: id, URL, type (`shared` / `dedicated`), hosting (`vendor` / `customer`), size, status (`active` / `draining` / `disabled`), capacity, wave.
- Tenant registry: id, name, tier, placement mode, assigned cell, status, limits.
- Placement strategy (pluggable). Default is **fill-first**: pick the fullest shared cell below a target utilization (e.g. 75%). The alternative, spread-evenly, gives a smaller blast radius at higher cost.
- Tenant move orchestration, seasonal scheduler, cell provisioner interface, audit log.

**Router (stateless)**
- Resolves tenant → routing entry, cached with a TTL and falling back to stale entries when needed.
- Forwards the request to the tenant's assigned cell.
- Per-tenant token-bucket rate limiting and usage metering.
- On `421 tenant_not_in_cell` it refreshes the route and retries once.
- Blocks `/internal/*` and strips internal headers from client requests.
- For the web UI's `/graphql`, takes the tenant from a verified session token instead of a client header (§2.4).

**Cell (identical deployable unit)**: a gateway, one database server, and one service stack per tenant (§3.4).
- **Cell gateway:** the cell's single entry point.
  - Keeps the registry of tenants the cell hosts and where each tenant's services run.
  - Routes `/v1/users…` → the tenant's user-account and `/v1/orders…` → the tenant's order-processing, and `/graphql` → the cell's `mgr` (§2.4).
  - Rejects tenants it doesn't host (`421`); frozen tenants are read-only (writes get `503`).
  - Caps requests handled at once, rejecting the excess immediately.
  - Stamps `X-Cell-ID` on every response.
  - Creates and drops tenant databases on the cell's database server.
  - Its `/internal/*` endpoints (provision, freeze, export, import, delete) require the cell's credential.
- **Cell database server** (Postgres): one per cell, **never shared between cells**. It holds one database per tenant of the cell.
- **Per-tenant service stack:** `user-account` and `order-processing`, the tenant's own containers, connected only to the tenant's own database.
- **`mgr`:** the web UI's GraphQL backend, one deployment per cell serving that cell's tenants through the gateway's `/v1` API (§2.4).

### 2.3 Control plane access

The control plane is never public. Callers, their APIs and credentials:

| Caller | API | Credential | Allowed |
|---|---|---|---|
| Operators | Admin API (`/v1/cells`, `/v1/tenants`, …) | Admin credential | Full admin |
| CI/CD workflows | Admin API | CI credential (AWS: GitHub OIDC role) | Scoped per route via IAM (e.g. report cell status, not move tenants) |
| Router | Routing API (`/v1/routing/*`), **read-only** | Router credential | Route lookups only; rejected on admin endpoints |
| Control plane → cells | Cell `/internal/*` | One credential per cell | Only that cell |

End users and tenants never reach the control plane.

**Locally:** `http://localhost:8000` (OpenAPI docs at `/docs`) with tokens from compose configuration, or the `cellctl` CLI.

**On AWS (stage 6):**
```
operators / CI ──SigV4 (IAM)──► API Gateway HTTP API ──VPC link──► control plane (private subnets)
router (platform cluster) ──in-cluster service──────────────────────► control plane
```
- Admin access goes through an **API Gateway HTTP API with IAM authorization** and a VPC link. It is pay per request with no fixed cost, reachable from GitHub-hosted runners without a VPN, scoped per route by IAM policy, and logged by CloudTrail and API Gateway access logs.
- The router reaches it inside the platform cluster with a read-only service identity.
- Break-glass and debugging: **SSM Session Manager port-forward** to the control plane task. It needs no bastion host or VPN, and is IAM-controlled and logged.
- Customers of customer-hosted cells have no access to the control plane.

**`cellctl` CLI:** wraps the admin API (`cellctl cells list`, `cellctl tenants create acme --tier premium`, and later `tenants move`, `schedules create`). It uses a token locally and IAM signing on AWS. Built in stage 1 and extended each stage.

### 2.4 Tenant web UI (`web-ui` + `mgr`)

A tenant's own users browse and manage their users and orders in a browser. They see only their own tenant's data. The UI follows the cell architecture: only static files are shared, and everything that serves a tenant's request runs inside that tenant's cell.

```
browser ─▶ ingress ─┬─ /          ─▶ web-ui (static files, platform cluster)
                    └─ /graphql   ─▶ router ─(tenant from the verified session)─▶ tenant's cell
                                                                                 └─▶ gateway ─▶ mgr ─▶ gateway /v1 ─▶ tenant's services
```

- **`web-ui` is a platform service:** static files only (no logic, no tenant data), in the platform cluster, with its own image, chart (`charts/web-ui`), release and autoscaler. Later it can sit behind a CDN.
- **`mgr` is a cell service:** it runs in **every cell's cluster** (one release per cell from its own chart, `charts/mgr`) and serves only the tenants of its cell. Each cell's `mgr` has its own autoscaler, so UI capacity grows with each cell added, and a tenant on a dedicated cell gets its own `mgr`. A bad `mgr` release or a heavy tenant affects one cell only.
- **The router stays thin:** for `/graphql` it verifies the session token and reads its tenant claim (no business logic), then routes and rate-limits exactly as for any request. A client's own `X-Tenant-ID` is ignored on that path. Because limits apply in the router, before `mgr`, one tenant can't exhaust another's UI capacity.
- **`mgr` reaches tenant data only through its cell's gateway `/v1` API,** for the session's tenant only. The gateway adds the tenant's stack credential, so `mgr` never holds one. It has no database and no access to the control plane or `/internal/*`, so the UI keeps working while the control plane is down. During a tenant move the gateway answers writes with `503 tenant_migrating`, and the UI shows the tenant as read-only until the switch.

**Why this shape (validated against the published standards):**
- AWS Well-Architected, *Reducing the Scope of Impact with Cell-Based Architecture*: a cell is *"a complete workload, with everything needed to operate independently"*; the router is *"the thinnest possible layer, with the responsibility of routing requests to the right cell, and only that"*, *"avoiding complex business logic within this layer"*; changes go out *"compartment by compartment"*. A GraphQL layer is business logic, so it belongs in the cells, not in the shared layer.
- Google Workspace SRE, *How to partition cloud applications to avoid global outages*: *"Each partition contains all the various servers necessary to service a user request from end to end"*, and changes are deployed *"to one partition at a time"*, canaried within a partition.
- A single shared `mgr` in the platform cluster (a common pattern without cells) was rejected: it would put application logic in the shared layer, make every `mgr` release and every heavy tenant affect all tenants, and give UI capacity a single ceiling.

**`web-ui` (front-end container)**
- React + TypeScript, Vite, pnpm (version pinned via Corepack). GraphQL client **urql**; its document cache refetches queries after a mutation (Graphcache can be added later). Types generated by GraphQL Code Generator from `mgr`'s schema.
- Screens: sign in (dev: pick a tenant), users list (paged), user detail with their orders, orders list, create user, delete user, place order, cancel order.
- Tests: Vitest + Testing Library with MSW faking `/graphql`; ESLint, `tsc --noEmit` and a coverage gate in CI.
- Image: Dockerfile target `web-ui`: a Node + pnpm build stage, then unprivileged nginx (pinned by digest) serving `dist/`. Source in `web-ui/` at the repo root.

**`mgr` (GraphQL backend container, in each cell)**
- Python 3.12, FastAPI + Strawberry; the usual `create_app(settings, http_client)` factory and `from_env()`. Source in `src/cellarch/mgr/`.
- Queries: `me`, `users(first, after)`, `user(id) { orders }`, `orders(userId, first, after)`, `order(id)`, all with cursor paging. Mutations: `createUser`, `deleteUser`, `createOrder`, `cancelOrder`, each passing an idempotency key through.
- Protection: DataLoader batching (a user's orders load in one batched call), query depth and cost limits, introspection off in production.
- Errors: the API's error shape goes into `errors[].extensions` (`code`, `status`, `Retry-After`).
- `/healthz`, plus `/readyz` (its cell gateway reachable). Structured logs with `request_id`, `tenant_id` and `cell_id`.

**Sign-in and tenant identity**
- Sign-in is the identity provider's job, behind one OIDC token interface: **Cognito** in production (a tenant claim in the token); locally a **dev identity provider** stand-in in the platform cluster that issues the same kind of signed token after you pick a tenant, clearly marked development-only.
- The token travels in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie on the single site; GraphQL mutations also need a custom request header, so a cross-site form can't trigger them.
- The router verifies the token's signature with the provider's public keys (no call per request), takes the tenant from the claim and sets `X-Tenant-ID`. Missing or invalid sessions get `401` before reaching any cell. The browser never sets the tenant.

**Kubernetes**
- `charts/web-ui` (platform cluster) and `charts/mgr` (every cell cluster): Deployment, Service, autoscaler (min and max replicas and target utilisation set per release), PodDisruptionBudget and NetworkPolicy, with resource requests and limits on every pod.
- Network access: `web-ui` makes no outbound calls; `mgr` accepts traffic only from its cell's gateway and can call only that gateway.
- Scheduling priority: the router above `web-ui` in the platform cluster; the gateway and tenant services above `mgr` in each cell, so UI load can't crowd out the API.
- Ingress routes by path on one hostname (`/` to `web-ui`, `/graphql` to the router), so no CORS setup is needed.
- Replicas: 1 each locally; in production at least 2 across availability zones for `web-ui` and for `mgr` in every cell.

**Acceptance criteria** (written test-first in the `mgr` and `web-ui` PRs):
- A tenant sees only its own users and orders, even when it asks for another tenant's id.
- Requests without a valid session are rejected by the router (`401`), and the browser can't override the tenant.
- Each cell's `mgr` serves only the tenants its cell hosts.
- A user's orders load in one batched call, not one call per user.
- A rate-limited tenant gets a GraphQL error with `Retry-After`.
- A create retried with the same idempotency key doesn't make a duplicate.
- Queries that are too deep or too costly are rejected.
- The UI keeps working when the control plane is down.
- During a tenant move, the UI shows the tenant as read-only, then works again after the switch, served by the new cell's `mgr`.
- Under load, a cell's `mgr` autoscaler adds pods in that cell (k3d smoke test).
- Front-end: screens render data, page through results, show errors, and run each mutation (Vitest).

**Independent delivery.** `web-ui` and `mgr` each ship on their own cadence, never waiting for the rest of the platform or for each other:
- **Own build:** each builds, tests and publishes only when its own files change (CI path filters: `web-ui/**`; `src/cellarch/mgr/**` plus its contract tests). `web-ui` has its own `package.json` and pnpm lockfile; `mgr` has its own Dockerfile target and dependency list.
- **Own version:** each has its own semantic version, and each image is tagged with it plus the git commit (`mgr:1.4.0-3f2a9c1`), never a shared tag. Each chart has its own chart version.
- **Own release and rollback (production: stage 7):**
  - `web-ui` is its own Argo CD Application, deployed as soon as it is green, outside the platform release; rolling it back redeploys its previous version.
  - `mgr` has its own pipeline that rolls out **cell by cell**, like the tenant services (§3.4): canary cell first, bake time, alarm checks, automatic rollback per wave. An `mgr` rollout never redeploys the gateway, the tenant services or the platform. Its approved version is in the per-environment versions file, so a new cell (`cellctl cells deploy`) gets it too.
- **Locally:** `make deploy-web-ui` builds `web-ui` and upgrades only its release in the platform cluster; `make deploy-mgr` builds `mgr`, loads it into every cell cluster with a fresh tag, and upgrades only the `mgr` release in each.
- **Contracts instead of lockstep:**
  - `mgr` depends only on the cell gateway's `/v1` API (the same contract the router exposes). Contract tests pin what it uses, so `mgr` and the tenant services can each deploy without the other.
  - `web-ui` depends only on `mgr`'s GraphQL schema. During an `mgr` wave different cells run different versions, so the schema changes additively: new fields first; removals only after a deprecation period, once no deployed `web-ui` uses them. A schema check in `mgr`'s CI rejects breaking changes against the published schema.
- **Runtime configuration:** `web-ui` reads its settings (GraphQL path, sign-in provider) from a `config.json` served next to it, never baked in at build time, so one image moves unchanged from local to staging to production.

**Production equivalent:** `web-ui` behind the router's existing ALB (with WAF) by path rule, CloudFront in front as an option for later; `/graphql` through the same ALB to the router, then each cell's internal NLB to its gateway and `mgr` in the cell's EKS cluster. No new load balancer. The router verifies Cognito tokens with Cognito's public keys.

**What is built when:** stage 4 builds everything that runs locally: both services, their charts, the router's session check, the dev identity provider, independent versions and image tags, CI path filters, the schema check and contract tests, and `make deploy-web-ui` / `make deploy-mgr`. All production parts come later: the ALB rules and Cognito with the infrastructure (stage 6), and each service's production pipeline (cell-by-cell waves for `mgr`) with delivery automation (stage 7).

**Cost:** `web-ui` is a few pods in the platform cluster. `mgr` adds a small deployment to every cell (in production at least 2 pods across availability zones), so its cost grows with the number of cells, as the rest of a cell does. No new AWS resources per cell beyond those pods. Cognito pricing to be confirmed in the infrastructure stage (6).

---

## 3. Partitioning and placement

### 3.1 Partition key
- **Tenant key:** `tenant_id`, sent as `X-Tenant-ID`. In production this is derived from the auth token or subdomain at the edge.

### 3.2 Routing
- A tenant lives in **exactly one cell** at any time. The routing entry is `tenant → cell` (plus status and limits).
- A tenant changes cell only through a tenant move (§5).

### 3.3 Placement modes

| Mode | v1 | Description |
|---|---|---|
| pooled | Built | Many tenants share a cell |
| dedicated | Built | One tenant, one cell (permanent, seasonal, or customer-hosted) |

**Out of scope:** splitting a single tenant across multiple cells (sharding). The largest unit of placement is one tenant on one dedicated cell, sized up via the cell profile (`small` / `medium` / `large`).

### 3.4 Tenant services and data

Tenants share a **cell** (up to its capacity, fill-first), but each tenant has **its own service containers and its own database**:

```
cell-1
├── gateway                         routes each tenant to its own stack
├── postgres                        one database server per cell (never shared across cells)
│     ├── database tenant_acme
│     │     ├── schema user_account       login acme_user_account: this schema only
│     │     └── schema order_processing   login acme_order_processing: this schema only
│     └── database tenant_globex …
├── acme-user-account  ◄──── acme-order-processing      (acme's containers)
└── globex-user-account ◄─── globex-order-processing    (globex's containers)
```

- **One database per tenant** holds all its data: users and orders. A backup, restore, move or deletion covers the whole tenant at once, consistent across both services.
- **One schema and login per service:** a service can only reach its own schema. Order-processing checks a user through user-account's API; there are no cross-service joins or foreign keys.
- **Per-tenant containers:**
  - **Fault isolation:** a crash in one tenant's services affects only that tenant.
  - **Resource caps per tier,** per service: standard 0.25 CPU / 256 MB, premium 1 CPU / 512 MB.
- **Noisy-neighbour limits in the shared database server:** each tenant login gets a connection limit and a statement timeout.
- **Onboarding is a handshake,** because tenants need containers and the control plane never gets Docker access:
  1. The control plane places the tenant (`provisioning`).
  2. The cell gateway creates the tenant database and logins.
  3. The operator tool (`cellctl` locally, the deployment pipeline on AWS) starts the tenant's containers.
  4. The control plane activates the tenant once they're healthy. Any failure undoes every step.
- **Deleting a tenant** stops its containers and drops its database.
- **On AWS (stage 6):** every cell gets **its own Aurora Serverless v2 PostgreSQL cluster**. A new cell means a new database server. Tenant databases live in their cell's cluster, and service passwords go to Secrets Manager.
- **Locally:** one Postgres container per cell. In-process tests use an SQLite stand-in behind the same storage interface; adapter tests run against real Postgres when `POSTGRES_URL` is set.
- **Replaces** the earlier `pooled`/`isolated` tier: every tenant now has its own containers and database.

**Independent delivery of the tenant services.** `user-account` and `order-processing` ship like `web-ui` and `mgr` (§2.4), each on its own cadence, but they run in every cell, so their rollouts still go cell by cell:
- **Own build:** each builds, tests and publishes only when its own files change (CI path filters: `src/cellarch/services/user_account/**`, `src/cellarch/services/order_processing/**`, plus the shared `services/common/**` for both).
- **Own version:** each has its own semantic version and an image tag with the git commit (`order-processing:2.3.0-3f2a9c1`), never a shared tag.
- **Own rollout, in waves (production: stage 7):** each service's pipeline rolls out its new version on its own: a canary cell first, bake time, alarm checks, then the next wave, with automatic rollback per wave. An `order-processing` rollout never redeploys `user-account`, the gateway or the platform.
- **One chart, separate versions:** the `tenant-stack` chart stays one release per tenant, but each service's image version is its own value (`userAccount.image.tag`, `orderProcessing.image.tag`). Upgrading one changes only its value, so only its pods roll.
- **Approved versions per environment:** a versions file per environment (`deploy/versions/<env>.yaml`) holds the version of each service that new stacks get. Each service's pipeline updates it after a successful rollout; `cellctl` reads it when it onboards or moves a tenant, and Argo CD rolls out from it (GitOps). A stack is never installed with an unreleased build.
- **Contracts instead of lockstep:** `order-processing` calls `user-account`'s API, so a contract test in both services' CI pins what `order-processing` uses. Either can deploy first.
- **Migrations that work with both versions (expand, then contract):** during a wave, old and new versions run side by side, so a schema change takes two releases. The first adds the new schema and keeps the old one working; a later release removes the old part once no running version uses it. Each service migrates only its own schema, with its own login.
- **Locally:** `make deploy-user-account` and `make deploy-order-processing` build only that image, load it into every cell cluster with a fresh tag, and upgrade only that service in each tenant's stack.

---

## 4. Rate limiting

- **Router (main):** a per-tenant token bucket (rate + burst).
- **Cell (backstop):** a cap on requests handled at once; when exceeded, new requests get `503` immediately.
- **Limits:** a default per tier (e.g. standard 50 rps / burst 100, premium 500 / 1000), a per-tenant override, and a seasonal override. Limits travel in the routing entry, so no extra lookups are needed and limits keep working while the control plane is down.
- **Responses:** `429` + `Retry-After`. All responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`.
- **Metering:** request and throttle counts per tenant at `/_router/stats`, exported as metrics. Sustained throttling is the signal to promote a tenant to a dedicated cell.

### 4.1 Algorithm
- **Token bucket** for API rate limiting: allows short bursts, answers immediately (`429`), and needs 2 numbers per tenant.
- **Leaky bucket is not used.** Its job (smoothing traffic into a fragile downstream) is covered by the cell's request cap.
- The limiter sits behind a `RateLimiter` interface, so another algorithm (e.g. a leaky bucket for a specific outbound integration) can be added without touching the rest.

### 4.2 Limiting across multiple routers

Three layers, all in v1:

| Layer | Default | Accuracy | Cost | Notes |
|---|---|---|---|---|
| **In-memory token bucket** | On | Approximate with N routers | None | Each router enforces limit ÷ N. N is read periodically from Kubernetes (ready router replicas), or set in config locally, so autoscaling doesn't distort limits. |
| **AWS WAF rate rule** keyed on `X-Tenant-ID` | On (AWS) | Coarse (minute-level windows), shared across all routers | Low | Shared edge backstop against floods; no new infrastructure. |
| **Redis-backed token bucket** (ElastiCache) | **Off**, per-environment flag | Exact, shared across routers | Redis cost only when enabled | For contractual or billing-grade limits. **Fails open:** if Redis is unreachable, the router falls back to the in-memory limiter, so Redis can never take traffic down. Atomic check-and-update via a Lua script. |

---

## 5. Tenant moves

One move procedure, reused for promote, demote, consolidate, seasonal and cross-account moves:

1. Mark the tenant `migrating` in the control plane (the router serves reads and pauses writes).
2. Freeze the tenant on the source cell (read-only).
3. Export from the source and import into the target.
4. Switch the routing entry to the target. Status becomes `active`.
5. Delete the tenant from the source. If this fails, flag the leftover data for cleanup.

On failure at any step: roll back (unfreeze the source, remove partial target data, set status back to `active`).

Stale router caches are safe: the source returns `421` after deletion, so the router refreshes the route and retries.

**How it runs.** A move is a record in the control plane that moves through fixed states. Like onboarding (§3.4), `cellctl` drives it (`cellctl tenants move acme --to cell-2`), because only the operator's tool installs Helm releases:

| Step | Call | State after |
|---|---|---|
| Start: check the target (active, another cell, has room counting moves already heading there), set the tenant `migrating`, freeze it on the source, create its database and secret on the target | `POST /v1/tenants/{id}/moves` | `started` |
| Install the tenant's stack in the target cell's cluster; its services create their tables | `cellctl` (Helm) | `started` |
| Copy: export from the source, import into the target, compare row counts (`409 tenant_stack_not_ready` until the target stack has created its tables; `cellctl` retries) | `POST /v1/moves/{id}/copy` | `copied` |
| Switch: the target checks the stack is ready, the route points to the target, the tenant is `active` | `POST /v1/moves/{id}/switch` | `switched` |
| Uninstall the stack on the source | `cellctl` (Helm) | `switched` |
| Complete: the source drops the tenant's database, logins and secret. If that fails the move still completes, with `cleanup_pending` set | `POST /v1/moves/{id}/complete` | `completed` |
| Roll back (any time before the switch): `cellctl` uninstalls the target stack, then the target's data is dropped, the source is unfrozen and the tenant is `active` again | `POST /v1/moves/{id}/rollback` | `rolled_back` |

- **One move per tenant at a time**, and only for `active` tenants.
- **Writes pause in two places:** the router answers writes for a `migrating` tenant with `503 tenant_migrating` (+ `Retry-After`) and forwards reads; the frozen source gateway does the same for routers whose cached route is still `active`.
- **After the switch the source is never unfrozen,** so a router with a stale route can only read identical data there; never write to the old copy.
- **The copy is a logical export** through the storage interface (`common/sql.py`), so the SQLite stand-in and Postgres behave the same:
  - JSON lines: a header (format version, tenant, each service's schema version), then batches of rows per table, then an end record with the row count of every table.
  - Every table of each service is copied (including idempotency keys, so a client retrying a create after the move still gets its original answer), except the migration records: the target's stack applies its own migrations, and the import refuses a different schema version (`409 schema_version_mismatch`).
  - Keys (`seq`) are kept, so cursors issued before the move stay valid; the target's sequences continue after the highest key.
  - The import refuses a target that already has data (`409 tenant_data_not_empty`), runs in one transaction per service, and works as each service's own login, so it can only write that service's schema.
  - The source only exports a frozen tenant (`409 tenant_not_frozen`), so the snapshot is consistent.
- **Who carries the data:** cells never call each other, so the control plane relays the stream from the source's `/internal/tenants/{id}/export` to the target's `/internal/tenants/{id}/import`, both with each cell's own credential. On AWS (stage 6) the control plane hands each cell a pre-signed S3 URL instead, so tenant data never passes through the control plane; the same S3 path carries cross-account moves (stage 8).
- **Audit log:** every admin change (cells, tenants, rate limits, every move step, including refused and failed ones) is recorded in the control plane with who, what, which tenant and cells, the outcome and the time. `GET /v1/audit` lists it newest first with cursor pagination and filters by tenant or cell; `cellctl audit` prints it.

Roadmap: near-zero-downtime moves via live replication / change data capture.

### 5.1 Cell lifecycle operations
- **Drain:** a `draining` cell receives no new tenants.
- **Consolidate:** move all tenants out of an under-used cell, then decommission it (cost saving).

All four operations below are `cellctl` workflows on top of the move procedure: only the operator's tool creates clusters and installs Helm releases.

| Command | What it does |
|---|---|
| `cellctl tenants promote acme [--cell cell-acme] [--size medium]` | Deploys a dedicated cell (capacity 1) and moves the tenant there. If the move fails, the new cell is removed again |
| `cellctl tenants demote acme [--delete-data]` | Moves the tenant to a shared cell chosen by placement, then decommissions its dedicated cell. Refused (nothing changes) when no shared cell has room |
| `cellctl cells drain cell-1` | Sets the cell `draining`, then moves its tenants out one at a time, each to a cell chosen by placement |
| `cellctl cells consolidate cell-3 [--delete-data]` | Drains a shared cell, then decommissions it |

- **Room is checked first:** drain and consolidate refuse up front (`no_capacity`) when the other shared active cells can't take every tenant, and refuse a cell with tenants that aren't `active` (`tenant_not_active`), so they never stop half-way for a reason known in advance.
- **A failure stops the drain:** that tenant's move is rolled back as usual, the drain reports the tenants left, and running it again carries on with them.
- **A dedicated cell holds exactly one tenant:** it is registered with capacity 1 (any other capacity is refused), placement never puts new tenants on it, and a move into it needs it empty.
- **`placement_mode` follows the cell:** the switch step sets it to `dedicated` on a dedicated cell and `pooled` on a shared one.
- **Defaults:** the dedicated cell is `cell-<tenant>`; demote and consolidate stop the emptied cell's cluster and keep its data, like `cells decommission` (`--delete-data` removes it).

---

## 6. Seasonal scaling

Move a tenant to a dedicated cell for a scheduled business event, then return it to a shared cell afterwards to save cost.

**Schedule (control-plane entity):**
```yaml
tenant_id: acme
window: 2026-11-25T00:00Z → 2026-12-02T00:00Z
lead_time: 12h                # move in early, during a quiet period
cooldown: 48h                 # stay through post-sale traffic
cell_profile: large
rate_limit_override: { rps: 2000, burst: 4000 }   # season-only, reverts automatically
return_to: shared             # fill-first picks the cell at return time, not necessarily the original
```

**Lifecycle (every step is saved and safe to repeat):**
```
scheduled → cell_provisioning → moving_in → seasonal_active
          → moving_out → cell_decommissioned → completed      (also: cancelled, failed)
```

**Components**
- **Scheduler:** a background loop inside the control plane, with an injectable clock for tests. Option: EventBridge Scheduler triggers on AWS.
- **Cell provisioner interface:**
  - `StandbyPoolProvisioner` (local): claims a pre-defined idle cell.
  - `GitHubWorkflowProvisioner` (AWS): triggers `cell-lifecycle.yml`.
- **Pre-scaling:** scheduled scaling (Karpenter node pools and the tenant's replicas) warms the seasonal cell before the window opens.

**Safety rules**
- Capacity check when the schedule is created and again when it runs. Overlapping schedules for one tenant are rejected.
- If no shared cell can take the tenant on return: open a new shared cell. If that fails, **keep the tenant dedicated and alert**. Availability wins over cost.
- Manual controls: start now, extend, cancel (cancelling a running schedule triggers an immediate move back).

**API**
```
POST   /v1/tenants/{id}/schedules
GET    /v1/tenants/{id}/schedules
POST   /v1/schedules/{sid}/extend
POST   /v1/schedules/{sid}/start-now
DELETE /v1/schedules/{sid}
```

---

## 7. Deployment models

| Model | Where it runs | v1 |
|---|---|---|
| 1. SaaS pooled | Our AWS | Built |
| 2. SaaS dedicated | Our AWS | Built |
| 3. **Customer-hosted cell (BYOC)** | Cell (app + data) in the customer's AWS account; our control plane and router | **Built** |
| 4. Fully self-hosted | Everything in the customer's account | Later (same artifacts, packaged for customer operation) |

### 7.1 Customer-hosted cell (v1)

**Cell definition**
```yaml
- id: cell-cust-globex
  type: dedicated
  hosting: customer
  account_id: "123456789012"
  region: eu-west-1
  role_arn: arn:aws:iam::123456789012:role/CellArchDeployRole
  external_id_ref: ssm:/cellarch/customers/globex/external-id
  connectivity: privatelink
  wave: 3
  maintenance_window: "sun 02:00-04:00 UTC"
```

**Customer setup**
- A **CloudFormation quick-start** (one click) and an equivalent **Terraform module**. Both create:
  - a cross-account deploy role, protected by an external ID, with least-privilege permissions limited to cell resources and a permission boundary;
  - a customer-managed KMS key;
  - a read-only role for operational metadata.
- The customer can revoke the role or the KMS key at any time.

**Connectivity**
- The cell is exposed via **AWS PrivateLink** (an NLB plus an endpoint service in the customer's account). Our router connects through an interface endpoint.
- No public exposure in the customer's account.
- Option: the customer has their own entry point. The cell's own limits and tenant checks still apply.

**Data ownership**
- The database, backups and logs stay in the customer's account, encrypted with the customer's KMS key.
- Only operational metadata leaves (health, metrics, version). What is shared is configurable.

**Software delivery**
- ECR images are shared cross-account (repository policy) or copied into the customer's ECR.
- Customer cells are in late release waves by default and honor their maintenance window.

**Moves**
- Onboarding to a customer cell: a cross-account move from a pooled cell. Data moves cell-to-cell through a pre-signed S3 export in the customer's account, so tenant data doesn't land in our account. This requires customer consent.

**Support**
- A break-glass access process: time-limited, customer-approved, fully logged.

**Security guardrails**
- We cannot apply SCPs to the customer's AWS organization. The cell stack ships its own guardrails (IAM boundary, encryption, Config rules), plus a checklist of what the customer must provide.

---

## 8. Infrastructure (AWS + Terraform/OpenTofu)

### 8.1 Accounts (AWS Organizations)
```
management
├── shared-services   ECR, Terraform state, GitHub OIDC roles
├── dev               2 shared cells, nightly shutdown
├── prod              control plane, router, cells
├── (optional) per-cell accounts for high-isolation cells
└── customer accounts (not ours; accessed via cross-account role)
```

### 8.2 Services

| Component | AWS service | Cost notes |
|---|---|---|
| Kubernetes clusters | **One EKS cluster per cell** + **one platform cluster** (router, control plane, Argo CD); Graviton nodes managed by Karpenter | One EKS fee per cluster: the price of real cell isolation; fill-first keeps cells few |
| Router | Deployment in the **platform cluster** across 3 AZs, behind **one** public ALB (AWS Load Balancer Controller), autoscaled | The only public entry point |
| Router → cell (vendor-hosted) | Each cell's gateway behind an **internal NLB** in its own cluster (or VPC Lattice) | Small per-cell cost; no cluster shared between cells |
| Router → cell (customer-hosted) | PrivateLink interface endpoint | Per-endpoint cost, built into customer pricing |
| Cell gateway | Deployment in the cell's own cluster (Helm `cell` chart) | Standby and seasonal cells' node groups scale to zero |
| Cell database | **One Aurora Serverless v2 PostgreSQL cluster per cell** (never shared across cells), one database per tenant, one schema and login per service | Auto-pause and scale to zero keep idle cells close to $0 |
| Tenant services | One Helm release of the `tenant-stack` chart per tenant in its cell's cluster: two Deployments (user-account, order-processing) sized by tier, own service account (IRSA), network policy | The price of per-tenant fault isolation |
| Control plane | Deployment in the platform cluster + its own small Aurora database | Off the request path |
| Delivery | **Helm charts** per service; **Argo CD** (GitOps) in the platform cluster deploys to every cell cluster in waves | No per-cell deployment tooling |
| Secrets in clusters | **External Secrets Operator** syncs from Secrets Manager | Nothing in git |
| Control plane admin access | API Gateway HTTP API (IAM auth) + VPC link; SSM Session Manager for break-glass | Pay per request; no bastion or VPN |
| Shared rate limiter (optional) | ElastiCache (Redis/Valkey), smallest node; created only when the flag is on | No cost when disabled |
| Secrets | SSM Parameter Store SecureString | Free standard tier |
| Observability | CloudWatch (7–14 day retention) + OpenTelemetry → CloudWatch / X-Ray | |
| Outbound network | VPC endpoints; dev in public subnets, no NAT | Avoids NAT fixed cost |
| Cost guardrails | AWS Budgets, Cost Anomaly Detection, required tags | Cost per cell and tenant |

### 8.3 Terraform layout
```
infra/
  bootstrap/            state bucket, GitHub OIDC roles (per account)
  customer-bootstrap/   Terraform module + CloudFormation quick-start for customer accounts
  modules/
    network/  shared/  control-plane/  router/  cell/  privatelink/
  stacks/
    platform/           network + shared + control-plane + router (per environment)
    cell/               one root module, applied once per cell
  envs/
    dev/   platform.tfvars  cells.yaml
    prod/  platform.tfvars  cells.yaml
```

- **Modules are account-agnostic.** Account, region, assume-role ARN and external ID are inputs.
- **One state file per cell** (`state/<env>/cells/<cell-id>.tfstate`), so infrastructure changes also roll out cell by cell.
- S3 backend with native locking (Terraform ≥ 1.10).
- Every resource is tagged with `cell_id`, `cell_type`, `hosting`, `tenant` (dedicated) and `env`.

### 8.4 Local environment

Locally, the topology mirrors production: **one small k3d (Kubernetes) cluster per cell plus one platform cluster**, on a shared Docker network.

```
k3d cluster "platform"   router (localhost:8080), control plane (localhost:8000), optional Redis
k3d cluster "cell-1"     cell gateway + the cell's own Postgres + each tenant's stack
k3d cluster "cell-2"     …
```

- The **same Helm charts** install locally and in production; only the values differ (image registry, sizes, secrets source).
- **`make up`:**
  - creates any missing clusters;
  - builds the images and loads them into the clusters;
  - installs the platform and seed cells with Helm.
- `cellctl cells deploy` creates a new cell **cluster**; `cellctl tenants create` installs a tenant's `tenant-stack` release in its cell's cluster.
- **Memory:** about 0.5–1 GB per cluster plus workloads. Two cells plus the platform fit in about 8 GB; 10–12 GB is recommended for Docker Desktop.
- Docker Compose is retired once the k3d runtime lands. In-process tests stay Docker-free.

### 8.5 Kubernetes runtime (cell = cluster)

Cells must share nothing that can fail together, including Kubernetes itself. So:

- **Each cell is its own Kubernetes cluster:** EKS in production, k3d locally. It has its own control plane, node groups, network plugin, DNS, ingress and add-ons. A bad cluster upgrade or add-on failure affects one cell.
- **The router and control plane run in a separate platform cluster,** never inside a cell cluster, so a cell cluster failure can't take the router down.
- **Cluster upgrades roll out cell by cell** in waves, like releases: canary cell cluster first.
- **Inside a cell cluster:**
  - the gateway and each tenant's stack are Helm releases;
  - **network policies** allow only gateway → tenant services, tenant services → their own database, and order-processing → its own user-account;
  - resource quotas and limits per tenant, by tier.
- **Packaging:** a Helm chart per service image (`router`, `control-plane`, `cell-gateway`, `user-account`, `order-processing`), plus umbrella charts `platform`, `cell` and `tenant-stack`. Charts are tested with `helm lint`, schema validation and helm-unittest.
- **Delivery:** **Argo CD** in the platform cluster deploys to every cell cluster from git, using ApplicationSets for cell-by-cell waves. Tenant onboarding commits the tenant's values file, Argo CD installs the release, and the control plane activates the tenant. A Kubernetes operator for tenants is the later enterprise step.
- **Portability:** the same charts run on any conformant Kubernetes. That includes a customer's own cluster for customer-hosted cells (stage 8) or another cloud.

---

## 9. CI/CD (GitHub Actions)

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | Pull request | Lint (ruff), type check (mypy), pytest with branch coverage gate (≥ 90%), `terraform test`, ARM Docker build, compose smoke test with demo, Terraform fmt/validate/tflint, Checkov, Trivy, pip-audit, gitleaks, Terraform plan comment per changed stack |
| `cd.yml` | Merge to main | Build once → push to ECR (SHA tag, signed) → apply platform → release cells in waves from `cells.yaml` (apply cell stack → Argo CD sync of the cell cluster → database changes → wait → CloudWatch alarm check → next wave; roll back the failed wave). Prod requires approval. Customer cells are in late waves within their maintenance windows. |
| `cell-lifecycle.yml` | Dispatched by the control plane (GitHub App token) or manually | Create or destroy a cell: edit `cells.yaml` (automated commit) → apply → deploy → health check → report to the control plane |
| `drift.yml` | Nightly | Plan every stack and alert on drift; report expired seasonal cells and untagged resources |

- Login: GitHub OIDC → IAM roles per environment. No long-lived keys.
- Database changes must stay compatible with the previous app version (expand/contract), so any cell can be rolled back.

### 9.1 PR approval: `github-actions[bot]` policy gate

PRs to `main` need one approval, given by `github-actions[bot]` through `pr-gate.yml` when all rules pass. It is a **policy gate, not a code review**: quality rests on the tests, coverage gate, scans and the TDD rule.

| # | Rule |
|---|---|
| R1 | CI passed for the PR's latest commit |
| R2 | Targets `main`, not a draft, not from a fork |
| R3 | TDD: `src/**` changes come with `tests/**` changes; `infra/modules/<m>/**` changes come with changes to that module's `*.tftest.hcl` |
| R4 | Protected paths (`.github/**`, `scripts/ci/**`) are never approved by the gate; a human reviews and merges with admin bypass |
| R5 | PR title follows conventional commits (`feat:`, `fix:`, `docs:`, `ci:`, `infra:`, `test:`, `refactor:`, `chore:`, `perf:`, `build:`); it becomes the squash-commit message |

- **Triggers:** `workflow_run` after CI completes, plus `pull_request_target` on title edits and ready-for-review. Both run the gate as defined on `main` and never check out or execute PR code, so a PR cannot change the rules that approve it.
- **Output:** one status comment per PR, updated in place. Approval is tied to the head commit; new pushes dismiss it and the gate re-evaluates after CI.
- **Implementation:** `scripts/ci/pr_gate.py` (rules as pure functions, unit-tested in `tests/ci/`).
- **Branch protection on `main`:** PR required, 1 approval, stale approvals dismissed, required check `lint-type-test`, squash merge only, no force pushes, admin bypass kept for protected paths and emergencies.

---

## 10. Security guardrails

| Layer | Guardrails |
|---|---|
| Tenant isolation | Single tenant-scoped data-access layer; Postgres row-level security; cell rejects tenants it doesn't host; cross-tenant read tests in CI |
| AWS organization | SCPs: deny disabling CloudTrail/GuardDuty/Config, deny unapproved regions, deny root actions, deny leaving the organization |
| Identity | OIDC only; one IAM role per cell, limited to its own database and secrets; permission boundaries on CI and provisioner roles; per-cell service credentials (IAM-signed / mTLS on AWS) |
| Network | Only the router's load balancer is public; cells in private subnets; security groups allow router→cell and control plane→cell only; **no cell-to-cell traffic**; AWS WAF on the load balancer |
| Data | KMS encryption at rest, TLS in transit, S3 Block Public Access across the account, point-in-time recovery backups, deletion protection in prod |
| Supply chain | Checkov, Trivy, pip-audit, Dependabot, gitleaks + push protection, branch protection, signed images, base images pinned by digest |
| Detection | Organization-wide CloudTrail, GuardDuty (prod), a small set of key AWS Config rules, IAM Access Analyzer, alerts via SNS |
| Application | Rate limits, input validation, request size limits, internal header stripping, `/internal/*` blocked at the router, control-plane audit log |
| Customer-hosted | Guardrails shipped in the cell stack, external ID, revocable role and KMS key, break-glass process |

---

## 11. Cost model (summary)

- Fill-first placement keeps the number of cells low. Consolidation retires under-used cells.
- Dedicated cells only for a paying tier, a scheduled season, or a customer-hosted deployment. They are demoted back to shared afterwards.
- Scale to zero: standby and seasonal cells (node groups at 0, Aurora auto-pause) and dev at night.
- Shared, not per cell: CI/CD, ECR, observability, control plane. Per cell: only the request path.
- No ALB, NAT or Redis per cell. Short log retention.
- Per-tenant metering plus tags give cost per tenant and per cell, which informs pricing.
- Customer-hosted cells: the customer pays for the infrastructure in their account.
- `docs/cost-model.md` will include AWS Pricing Calculator estimates for dev and a typical prod setup.

---

## 12. Repository layout

```
src/cellarch/
  common/                         headers, contracts, auth, db, logging
  control_plane/                  app, store, placement, moves, scheduler, provisioners, audit
  data_plane/
    router/                       app, routing cache/resolver, rate limiter, metering, proxy
    cell/                         app, store (tenant-scoped), concurrency limiter
  cli/                            cellctl (admin CLI)
tests/
scripts/demo.py
infra/                            see §8.3
.github/workflows/                ci.yml, cd.yml, cell-lifecycle.yml, drift.yml
deploy/docker-compose.yml
Dockerfile
docs/
  plan.md  architecture.md  cost-model.md  security.md  deployment-models.md
  runbooks/  cell-rollout.md  seasonal-scaling.md  customer-onboarding.md  break-glass.md
```

Stack: Python 3.12, FastAPI, httpx, pydantic, pytest. SQLite locally, Aurora PostgreSQL on AWS.

---

## 13. Delivery stages

| # | Stage | Main outputs |
|---|---|---|
| 1 | Core platform | Cells, router (tenant routing, cache, stale-route fallback, retry), control plane (registry, fill-first placement, separate admin/router/per-cell credentials), tenant-isolation data layer, `cellctl` (cells and tenants), Dockerfile, compose, demo. CI jobs for Docker build and compose smoke test follow in a separate `ci:` PR (protected path). |
| 2 | Traffic protection | `RateLimiter` interface, in-memory token bucket with automatic N-router share, Redis-backed limiter (off by default, fails open), tier and override limits, metering, cell request cap. WAF rate rule follows in stage 6. |
| 2.5 | Tenant services | Cell gateway; per-tenant `user-account` and `order-processing` containers; one Postgres server per cell with a database per tenant and a schema and login per service; onboarding handshake and tenant delete; replaces the sample `items` API and the isolation tier (§3.4) |
| 2.6 | Kubernetes runtime | One image per service; Helm charts (`platform`, `cell`, `tenant-stack` and per service) tested with lint, schema validation and helm-unittest; local k3d clusters (platform + one per cell); `make up` and `cellctl` driving Helm; CI smoke test on k3d; Docker Compose retired (§8.5) |
| 3 | Tenant moves | **3a:** move procedure (start the tenant's stack on the target cell, copy its database, switch, roll back) and audit log. **3b:** promote/demote, drain, consolidate |
| 4 | Tenant web UI | `web-ui` (React + TypeScript, urql, Vite, pnpm) as a platform service and `mgr` (GraphQL over the cell gateway's `/v1` API: cursor paging, DataLoader batching, depth and cost limits, idempotency keys) as a cell service in every cell; the router's session check (tenant from a verified token) and a local dev identity provider; Dockerfile targets and their own charts (`charts/web-ui`, `charts/mgr`) with autoscaler, PodDisruptionBudget and NetworkPolicy; `make up` exposing the UI on one local port, and `make deploy-web-ui` / `make deploy-mgr` to ship either one alone; independent versions, image tags and CI path filters, a GraphQL schema compatibility check and `/v1` contract tests; independent delivery for `user-account` and `order-processing` too: own versions, image tags and CI path filters, separate image values in `tenant-stack`, the per-environment versions file used by `cellctl`, their contract test and `make deploy-user-account` / `make deploy-order-processing` (all production pipelines, with per-service cell-by-cell waves for `mgr` and the tenant services, are stage 7); CI jobs for pnpm lint, type-check, test and coverage in a separate `ci:` PR (§2.4, §3.4) |
| 5 | Seasonal scaling | Schedules, scheduler, standby-pool provisioner, season limits. (Local cell deploy/decommission via `cellctl` was pulled forward into stage 1; stage 5 builds the scheduler-driven provisioner on top.) |
| 6 | Infrastructure | Terraform bootstrap, modules, platform and cell stacks (an **EKS cluster and Aurora cluster per cell**, a platform EKS cluster), dev environment, security guardrails, control plane admin access (API Gateway + IAM, SSM runbook), `cellctl` IAM signing, Terraform checks in CI |
| 7 | Delivery automation | **Argo CD** (ApplicationSets, cell-by-cell waves), **independent production pipelines**: one per tenant service (`user-account`, `order-processing`) with its own cell-by-cell waves, and `mgr` with its own cell-by-cell waves, and `web-ui` as its own Argo CD Application outside the platform release; the per-environment versions file updated by each pipeline; External Secrets, `cd.yml` building and signing images, `cell-lifecycle.yml`, GitHub-workflow provisioner, `drift.yml` |
| 8 | Customer-hosted cells | `hosting` field and per-cell credentials (app), customer-bootstrap (CloudFormation + Terraform), PrivateLink module, cross-account image sharing, cross-account move via S3, metadata-only telemetry, maintenance windows, break-glass runbook |
| 9 | Documentation | Architecture, cost model, security, deployment models, runbooks |

Account-agnostic modules and the cell `hosting` field are introduced in stages 1 and 6 so stage 8 needs no rework.

---

## 14. Development approach: test-driven development (TDD)

All code, application and infrastructure, is written test-first.

### 14.1 The cycle
1. **Red:** write one small failing test for the next behavior. Run it and see it fail for the expected reason.
2. **Green:** write the minimum code to make it pass.
3. **Refactor:** clean up with all tests green.
4. Repeat. Each behavior in this plan becomes one or more tests before it becomes code.

### 14.2 Outside-in per stage
- Each stage **starts with acceptance tests** taken from this plan's behaviors (e.g. "a tenant on a frozen cell can read but not write"). They are written first and fail until the stage is done.
- Inside that, unit-level TDD drives each module (placement, token bucket, cache, move steps, scheduler states).
- A stage is done when its acceptance tests pass.
- Criteria are listed in plain language in [acceptance-criteria.md](acceptance-criteria.md), one ID per criterion matching a `test_<id>_…` acceptance test. `tests/acceptance/test_traceability.py` fails CI if the two drift apart.

### 14.3 Test layers

| Layer | What | Tooling | Speed |
|---|---|---|---|
| Unit | Pure logic: placement strategies, token bucket, N-router share, routing cache, schedule state machine, move-step rollback | pytest, injected clock, in-memory fakes | Milliseconds |
| Contract | Wire contracts between router ↔ control plane and control plane ↔ cell (routing entry, `/internal` API); both sides tested against the same schemas | pytest + shared pydantic models | Fast |
| In-process integration | The full platform wired with httpx ASGI transports, no Docker: onboarding, routing, isolation, `/internal` blocking, rate limits, stale-route fallback, cell outage blast radius, moves and rollback, the seasonal lifecycle, customer-cell credential isolation | pytest | Seconds |
| Adapter | Redis limiter including fail-open; the storage interface and the tenant services (A42–A49) on PostgreSQL | pytest against Postgres 17 and Redis service containers in CI (`POSTGRES_URL`, `REDIS_URL`); skipped locally without them | Seconds |
| Smoke | `make up` (k3d clusters) + `scripts/demo.py` | CI | Minutes |
| Infrastructure | Module behavior via native `terraform test` (mocked providers, no AWS cost); security guardrails as policy tests (Checkov custom checks) | terraform test, Checkov, tflint | Fast |
| Infrastructure (optional) | Real apply in the dev account | Terratest | Minutes |

### 14.4 Rules
- **No production code without a failing test first.** Bug fixes start with a test that reproduces the bug.
- **Tests never sleep.** Time comes from an injectable clock.
- **Fakes over mocks:** in-memory provisioner, routing store and HTTP transports. Mock only at external boundaries (AWS, GitHub).
- **Tenant-isolation tests are mandatory** for any change touching data access.
- **Commit history shows the cycle:** a failing test, then the implementation (as separate commits or a single commit containing both, never code without tests).
- **CI gates:** all tests pass; branch coverage ≥ 90% on `src/` (pytest-cov). Coverage is a safety net, not the goal.
- Mutation testing (mutmut) runs nightly on core modules (placement, limiter, move procedure) to check that tests actually catch faults.

---

## 15. Roadmap (after v1)

- Automatic promote/demote driven by metering.
- Near-zero-downtime moves via live replication / change data capture.
- Fully self-hosted package (deployment model 4).
- Multi-region router and control plane.

---

## 16. Decisions log

| Decision | Choice |
|---|---|
| Language / framework | Python 3.12 + FastAPI |
| Cloud | AWS |
| IaC | Terraform / OpenTofu |
| CI/CD | GitHub Actions |
| Local storage / cloud storage | SQLite / Aurora Serverless v2 PostgreSQL |
| Compute | **Kubernetes:** EKS (Graviton, Karpenter) in production, k3d locally. Replaces the earlier ECS Fargate choice, so cells can run outside AWS (customer clusters, other clouds) |
| Cell boundary in Kubernetes | **One cluster per cell** (production and local); router and control plane in a separate platform cluster. Namespace-per-cell was rejected: a shared cluster is a shared failure domain |
| Deployment | **Helm** charts per service + umbrella charts; **Argo CD** GitOps with cell-by-cell waves; External Secrets for Secrets Manager |
| Images | One image per service (`router`, `control-plane`, `cell-gateway`, `user-account`, `order-processing`) from one multi-stage Dockerfile |
| Local runtime | k3d clusters (platform + one per cell) on a shared Docker network; Docker Compose retired once this lands |
| Default placement | Fill-first (target utilization 75%); when every shared cell is past the target, keep placing into the remaining room (emptiest first) with a capacity-low warning; `503` only when all are full |
| Partition key | `tenant_id` only; a tenant lives in exactly one cell |
| Sharding a tenant across cells | Excluded (not in v1 or roadmap) |
| Tenant services and data | Each tenant has its own `user-account` and `order-processing` containers and its own database, with one schema and login per service; tenants share cells up to capacity (§3.4). Replaces the pooled/isolated tier |
| Database server | One per cell, never shared across cells: Postgres container locally, Aurora Serverless v2 cluster on AWS. A new cell gets a new database server |
| Control plane storage | Stays SQLite for now (no tenant data); can move to Postgres later |
| Tenant resource caps | Per service: standard 0.25 CPU / 256 MB, premium 1 CPU / 512 MB |
| Order statuses | `placed` → `cancelled` |
| Development approach | Test-driven development (red → green → refactor), outside-in per stage |
| Rate-limit algorithm | Token bucket (no leaky bucket) |
| Shared rate limiter | `RATE_LIMIT_BACKEND=redis`: atomic Lua token bucket using Redis's clock, one bucket per tenant shared by all routers (full limit, not split); fails open to the in-memory limiter with a 5 s circuit breaker and 50 ms Redis timeouts; local `redis` compose profile; ElastiCache on AWS (stage 6) |
| Usage metering | Router counts requests, throttles and status classes per known tenant (in memory, per replica); `/_router/stats` behind a separate metrics token; `cellctl usage` |
| Cell request cap | `MAX_CONCURRENT_REQUESTS` (default 100) per cell; over it, `503 cell_overloaded` immediately, no queueing; `/healthz` exempt |
| Default rate limits | standard 50 requests/s burst 100, premium 500/s burst 1000 (`RATE_LIMIT_STANDARD` / `RATE_LIMIT_PREMIUM`); per-tenant override via `cellctl tenants set-limit`; limits travel in the routing entry |
| Rate limiting across routers | In-memory with automatic N-router share + WAF rate rule; Redis-backed limiter built, off by default, fails open |
| PR approver | `github-actions[bot]` policy gate (§9.1); no AI review, no secrets |
| Control plane access | Never public; separate admin, router (read-only) and per-cell credentials; AWS: API Gateway HTTP API with IAM auth + VPC link, SSM for break-glass; `cellctl` CLI |
| Cell credentials | Write-only; registered through the API and stored in the control plane database locally, SSM Parameter Store (KMS) on AWS; `CELL_CREDENTIALS` remains as a configuration fallback |
| Cell registration | Control plane verifies the cell first: healthy, reports the expected cell ID, accepts the credential |
| Local cell deploy | `cellctl cells deploy/decommission` drives Docker on the operator's machine; the control plane never gets the Docker socket (it would mean root on the host). On AWS the same commands trigger `cell-lifecycle.yml` (stage 7) |
| Merge strategy | Squash merge only; PR title (conventional commits) becomes the commit message |
| Customer-hosted cells | Built in v1 (model 3); fully self-hosted later |
| Tenant stacks in a cell | The cell gateway creates each tenant's database and a login per service on the cell's Postgres (a schema per service, connection limit, statement timeout) and writes the stack's Kubernetes Secret (its service account may create, replace and delete Secrets, never read them); `cellctl` installs the `tenant-stack` release and activates the tenant; the gateway forwards `/v1/users` and `/v1/orders` with the tenant's stack credential. The cell's Postgres admin password is generated in the cluster and never leaves it. Replaces the sample `items` API and the pooled/isolated tier |
| Tenant moves | A move is a control-plane record with fixed states (`started` → `copied` → `switched` → `completed`, or `rolled_back`), driven step by step by `cellctl` (Helm installs) like onboarding. The copy is a logical JSON-lines export through the storage interface (works on SQLite and Postgres, keeps keys, refuses a schema-version mismatch), relayed by the control plane because cells never call each other; pre-signed S3 URLs on AWS (stage 6) so data bypasses the control plane. The router and the frozen source both pause writes; the source is never unfrozen after the switch. `pg_dump` was rejected: it needs Postgres client tools in the gateway image and can't run against the in-process stand-in |
| Dedicated cells | One tenant per dedicated cell (capacity 1, enforced at registration and on moves); `placement_mode` is set by the switch from the target cell's type. Promote, demote, drain and consolidate are `cellctl` workflows over the move procedure, with up-front room checks |
| GraphQL backend for the UI | `mgr` (FastAPI + Strawberry) serves one GraphQL endpoint to `web-ui`, so a screen gets what it needs in one round trip; DataLoader batching, depth and cost limits and no production introspection keep it cheap and safe. The tenant services keep their REST `/v1` API |
| UI services | `web-ui` (static files) is a platform service; `mgr` (GraphQL) is a **cell service** that runs in every cell's cluster and serves only that cell's tenants. Each has its own image, chart, release and autoscaler. Validated against AWS Well-Architected (*Reducing the Scope of Impact with Cell-Based Architecture*: a cell is a complete workload; the router is the thinnest possible layer without business logic; changes go out cell by cell) and Google Workspace SRE (*How to partition cloud applications to avoid global outages*: each partition contains every server needed end to end; one partition at a time). A single shared `mgr` in the platform cluster was rejected: application logic in the shared layer, all tenants hit by one bad release or one heavy tenant, and a single capacity ceiling |
| Tenant service delivery | `user-account` and `order-processing` are independently deployable like the UI services (own path filters, version, image tag and pipeline), but roll out cell by cell per service (canary cell, bake, alarms, automatic rollback). One `tenant-stack` release per tenant with a separate image value per service; a per-environment versions file tells `cellctl` and Argo CD which versions new stacks get; contract test between them; schema changes are expand-then-contract so two versions can run side by side |
| UI delivery | Each of `web-ui` and `mgr` is independently deployable: own CI path filters, semantic version and image tag (`<version>-<commit>`). `web-ui` is its own Argo CD Application outside the platform release; `mgr` has its own pipeline rolling out cell by cell (canary cell, bake, alarms, automatic rollback), with its version in the per-environment versions file. `make deploy-web-ui` / `make deploy-mgr` locally. Coupling is replaced by contracts: `mgr` pins the gateway `/v1` API it uses with contract tests; its GraphQL schema changes additively (cells can run different `mgr` versions during a wave) and a schema check rejects breaking changes; `web-ui` takes runtime configuration |
| UI calls go through the router | `/graphql` reaches a cell like any request: the router routes and rate-limits it per tenant, so one tenant can't exhaust another's UI capacity, and stale routes, moves and freezes behave as for the API. Inside the cell, `mgr` reaches tenant data only through its gateway's `/v1` API, which adds the stack credential; `mgr` has no database, no stack credentials and no access to the control plane or `/internal/*` |
| Tenant identity in the UI | From a verified session, never the browser: an OIDC token with a tenant claim (Cognito in production; a development-only identity provider stand-in locally) in an `HttpOnly`, `SameSite=Strict` cookie. The router verifies its signature with the provider's public keys and sets `X-Tenant-ID` (`401` without a valid session): token checking, not business logic, so the router stays the thinnest layer |
| UI GraphQL client | urql: small, with a document cache that refetches after mutations; Graphcache can be added later |
| Front-end package manager | pnpm, version pinned via Corepack |
| Audit log | Control-plane table of every admin change and move step (actor, action, tenant, cell, outcome, detail, time), newest first via `GET /v1/audit` with cursor pagination. CloudWatch Logs copy on AWS (stage 6) |
| Tenant service storage | One `Database` interface (`common/sql.py`) chosen by `DATABASE_URL`: PostgreSQL (psycopg 3) in cells and on Aurora, SQLite as the in-process stand-in. Portable SQL (`?` placeholders, dict rows, `{seq_pk}` identity keys), driver errors mapped to `IntegrityError` / `DatabaseError`, migrations serialised by a per-namespace lock (Postgres advisory lock). Postgres major version 17 to match Aurora |
