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

**Cell (identical deployable unit)**: a gateway, one database server, and one service stack per tenant (§3.4).
- **Cell gateway:** the cell's single entry point.
  - Keeps the registry of tenants the cell hosts and where each tenant's services run.
  - Routes `/v1/users…` → the tenant's user-account and `/v1/orders…` → the tenant's order-processing.
  - Rejects tenants it doesn't host (`421`); frozen tenants are read-only (writes get `503`).
  - Caps requests handled at once, rejecting the excess immediately.
  - Stamps `X-Cell-ID` on every response.
  - Creates and drops tenant databases on the cell's database server.
  - Its `/internal/*` endpoints (provision, freeze, export, import, delete) require the cell's credential.
- **Cell database server** (Postgres): one per cell, **never shared between cells**. It holds one database per tenant of the cell.
- **Per-tenant service stack:** `user-account` and `order-processing`, the tenant's own containers, connected only to the tenant's own database.

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

**On AWS (stage 5):**
```
operators / CI ──SigV4 (IAM)──► API Gateway HTTP API ──VPC link──► control plane (private subnets)
router (platform cluster) ──in-cluster service──────────────────────► control plane
```
- Admin access goes through an **API Gateway HTTP API with IAM authorization** and a VPC link. It is pay per request with no fixed cost, reachable from GitHub-hosted runners without a VPN, scoped per route by IAM policy, and logged by CloudTrail and API Gateway access logs.
- The router reaches it inside the platform cluster with a read-only service identity.
- Break-glass and debugging: **SSM Session Manager port-forward** to the control plane task. It needs no bastion host or VPN, and is IAM-controlled and logged.
- Customers of customer-hosted cells have no access to the control plane.

**`cellctl` CLI:** wraps the admin API (`cellctl cells list`, `cellctl tenants create acme --tier premium`, and later `tenants move`, `schedules create`). It uses a token locally and IAM signing on AWS. Built in stage 1 and extended each stage.

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
- **On AWS (stage 5):** every cell gets **its own Aurora Serverless v2 PostgreSQL cluster**. A new cell means a new database server. Tenant databases live in their cell's cluster, and service passwords go to Secrets Manager.
- **Locally:** one Postgres container per cell. In-process tests use an SQLite stand-in behind the same storage interface; adapter tests run against real Postgres when `POSTGRES_URL` is set.
- **Replaces** the earlier `pooled`/`isolated` tier: every tenant now has its own containers and database.

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

Roadmap: near-zero-downtime moves via live replication / change data capture.

### 5.1 Cell lifecycle operations
- **Drain:** a `draining` cell receives no new tenants.
- **Consolidate:** move all tenants out of an under-used cell, then decommission it (cost saving).

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
- **Portability:** the same charts run on any conformant Kubernetes. That includes a customer's own cluster for customer-hosted cells (stage 7) or another cloud.

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
| 2 | Traffic protection | `RateLimiter` interface, in-memory token bucket with automatic N-router share, Redis-backed limiter (off by default, fails open), tier and override limits, metering, cell request cap. WAF rate rule follows in stage 5. |
| 2.5 | Tenant services | Cell gateway; per-tenant `user-account` and `order-processing` containers; one Postgres server per cell with a database per tenant and a schema and login per service; onboarding handshake and tenant delete; replaces the sample `items` API and the isolation tier (§3.4) |
| 2.6 | Kubernetes runtime | One image per service; Helm charts (`platform`, `cell`, `tenant-stack` and per service) tested with lint, schema validation and helm-unittest; local k3d clusters (platform + one per cell); `make up` and `cellctl` driving Helm; CI smoke test on k3d; Docker Compose retired (§8.5) |
| 3 | Tenant moves | Move procedure (start the tenant's stack on the target cell, copy its database, switch), promote/demote, drain, consolidate, audit log |
| 4 | Seasonal scaling | Schedules, scheduler, standby-pool provisioner, season limits. (Local cell deploy/decommission via `cellctl` was pulled forward into stage 1; stage 4 builds the scheduler-driven provisioner on top.) |
| 5 | Infrastructure | Terraform bootstrap, modules, platform and cell stacks (an **EKS cluster and Aurora cluster per cell**, a platform EKS cluster), dev environment, security guardrails, control plane admin access (API Gateway + IAM, SSM runbook), `cellctl` IAM signing, Terraform checks in CI |
| 6 | Delivery automation | **Argo CD** (ApplicationSets, cell-by-cell waves), External Secrets, `cd.yml` building and signing images, `cell-lifecycle.yml`, GitHub-workflow provisioner, `drift.yml` |
| 7 | Customer-hosted cells | `hosting` field and per-cell credentials (app), customer-bootstrap (CloudFormation + Terraform), PrivateLink module, cross-account image sharing, cross-account move via S3, metadata-only telemetry, maintenance windows, break-glass runbook |
| 8 | Documentation | Architecture, cost model, security, deployment models, runbooks |

Account-agnostic modules and the cell `hosting` field are introduced in stages 1 and 5 so stage 7 needs no rework.

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
| Adapter | Redis limiter including fail-open; Postgres storage including row-level security | pytest against Redis/Postgres service containers in CI | Seconds |
| Smoke | `docker compose up` + `scripts/demo.py` | CI | Minutes |
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
| Shared rate limiter | `RATE_LIMIT_BACKEND=redis`: atomic Lua token bucket using Redis's clock, one bucket per tenant shared by all routers (full limit, not split); fails open to the in-memory limiter with a 5 s circuit breaker and 50 ms Redis timeouts; local `redis` compose profile; ElastiCache on AWS (stage 5) |
| Usage metering | Router counts requests, throttles and status classes per known tenant (in memory, per replica); `/_router/stats` behind a separate metrics token; `cellctl usage` |
| Cell request cap | `MAX_CONCURRENT_REQUESTS` (default 100) per cell; over it, `503 cell_overloaded` immediately, no queueing; `/healthz` exempt |
| Default rate limits | standard 50 requests/s burst 100, premium 500/s burst 1000 (`RATE_LIMIT_STANDARD` / `RATE_LIMIT_PREMIUM`); per-tenant override via `cellctl tenants set-limit`; limits travel in the routing entry |
| Rate limiting across routers | In-memory with automatic N-router share + WAF rate rule; Redis-backed limiter built, off by default, fails open |
| PR approver | `github-actions[bot]` policy gate (§9.1); no AI review, no secrets |
| Control plane access | Never public; separate admin, router (read-only) and per-cell credentials; AWS: API Gateway HTTP API with IAM auth + VPC link, SSM for break-glass; `cellctl` CLI |
| Cell credentials | Write-only; registered through the API and stored in the control plane database locally, SSM Parameter Store (KMS) on AWS; `CELL_CREDENTIALS` remains as a configuration fallback |
| Cell registration | Control plane verifies the cell first: healthy, reports the expected cell ID, accepts the credential |
| Local cell deploy | `cellctl cells deploy/decommission` drives Docker on the operator's machine; the control plane never gets the Docker socket (it would mean root on the host). On AWS the same commands trigger `cell-lifecycle.yml` (stage 6) |
| Merge strategy | Squash merge only; PR title (conventional commits) becomes the commit message |
| Customer-hosted cells | Built in v1 (model 3); fully self-hosted later |
