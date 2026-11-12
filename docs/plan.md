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

**Cell (identical deployable unit)**
- A sample domain API (`workspaces`, `items`) with its own database.
- Every query is tenant-scoped through a single data-access layer.
- Rejects tenants it doesn't host (`421`). Frozen tenants are read-only (writes get `503`).
- A cap on requests handled at once, with immediate rejection when full.
- Stamps `X-Cell-ID` on every response.
- `/internal/*` endpoints (provision, freeze, export, import, delete) require per-cell credentials.

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
| **In-memory token bucket** | On | Approximate with N routers | None | Each router enforces limit ÷ N. N is read periodically from ECS (running task count), or set in config locally, so autoscaling doesn't distort limits. |
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
- **Pre-scaling:** scheduled ECS scaling warms the seasonal cell before the window opens.

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
| Router | ECS Fargate (Graviton) behind **one** public ALB, autoscaled | The only public entry point |
| Router → cell (vendor-hosted) | ECS Service Connect (Cloud Map) | No load balancer per cell |
| Router → cell (customer-hosted) | PrivateLink interface endpoint | Per-endpoint cost, built into customer pricing |
| Cell app | ECS Fargate (Graviton) per cell | Standby and seasonal cells at 0 tasks |
| Cell database | Aurora Serverless v2 PostgreSQL with auto-pause | Pay for load; idle cells close to $0 |
| Small shared cells | Shared Aurora cluster, a separate database per cell | Lower isolation level, cheaper |
| Control plane | 1 small Fargate task + its own small Aurora database | Off the request path |
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
- docker compose: control-plane, router (8080), cell-1, cell-2 (shared), cell-3 (standby pool), and cell-cust (a simulated customer-hosted cell on an isolated network with its own credentials). An optional `redis` compose profile runs Redis for testing the shared rate limiter.
- SQLite per cell. Postgres is swapped in on AWS through the same storage interface.

---

## 9. CI/CD (GitHub Actions)

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | Pull request | Lint (ruff), type check (mypy), pytest with branch coverage gate (≥ 90%), `terraform test`, ARM Docker build, compose smoke test with demo, Terraform fmt/validate/tflint, Checkov, Trivy, pip-audit, gitleaks, Terraform plan comment per changed stack |
| `cd.yml` | Merge to main | Build once → push to ECR (SHA tag, signed) → apply platform → release cells in waves from `cells.yaml` (apply cell stack → ECS deploy → database changes → wait → CloudWatch alarm check → next wave; roll back the failed wave). Prod requires approval. Customer cells are in late waves within their maintenance windows. |
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
- Scale to zero: standby and seasonal cells (ECS at 0 tasks, Aurora auto-pause) and dev at night.
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
| 1 | Core platform | Cells, router (tenant routing, cache, stale-route fallback, retry), control plane (registry, fill-first placement), tenant-isolation data layer, compose, `ci.yml` app checks |
| 2 | Traffic protection | `RateLimiter` interface, in-memory token bucket with automatic N-router share, Redis-backed limiter (off by default, fails open), tier and override limits, metering, cell request cap. WAF rate rule follows in stage 5. |
| 3 | Tenant moves | Move procedure, promote/demote, drain, consolidate, audit log |
| 4 | Seasonal scaling | Schedules, scheduler, standby-pool provisioner, season limits |
| 5 | Infrastructure | Terraform bootstrap, modules, platform and cell stacks, dev environment, security guardrails, Terraform checks in CI |
| 6 | Delivery automation | `cd.yml` with release waves, `cell-lifecycle.yml`, GitHub-workflow provisioner, `drift.yml` |
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
| Compute | ECS Fargate (Graviton) |
| Default placement | Fill-first (target utilization 75%); when every shared cell is past the target, keep placing into the remaining room (emptiest first) with a capacity-low warning; `503` only when all are full |
| Partition key | `tenant_id` only; a tenant lives in exactly one cell |
| Sharding a tenant across cells | Excluded (not in v1 or roadmap) |
| Development approach | Test-driven development (red → green → refactor), outside-in per stage |
| Rate-limit algorithm | Token bucket (no leaky bucket) |
| Rate limiting across routers | In-memory with automatic N-router share + WAF rate rule; Redis-backed limiter built, off by default, fails open |
| PR approver | `github-actions[bot]` policy gate (§9.1); no AI review, no secrets |
| Merge strategy | Squash merge only; PR title (conventional commits) becomes the commit message |
| Customer-hosted cells | Built in v1 (model 3); fully self-hosted later |
