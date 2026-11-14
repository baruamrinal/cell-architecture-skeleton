# CLAUDE.md

Reference implementation of cell-based architecture for a multi-tenant SaaS platform, with automated AWS infrastructure (Terraform/OpenTofu) and CI/CD (GitHub Actions).

**Source of truth: [docs/plan.md](docs/plan.md).** Read it before making design or code changes. If a change conflicts with the plan, raise it and update the plan first. Don't silently diverge.

## Status

Stages 1, 2, 2.5, 2.6 and 3 implemented: core platform, traffic protection, per-tenant services (each tenant's own user-account + order-processing containers and database on its cell's Postgres), the Kubernetes runtime (k3d locally: a platform cluster + one cluster per cell; Helm charts `platform`, `cell`, `tenant-stack`), and tenant moves (move procedure with rollback, audit log, promote/demote, drain, consolidate). Stage 4 in progress (tenant web UI, docs/plan.md §2.4): `mgr` (GraphQL backend in every cell), the router's tenant-host session check and the dev identity provider are built in-process; their charts and `make deploy-mgr` come next, then `web-ui`. Seasonal scaling is now stage 5; its work in progress is parked on branch `feat/seasonal-schedules`. Work proceeds in the delivery stages in docs/plan.md §13, one stage at a time.

## Working agreements

- **Plan before code.** Propose the approach for a stage or a significant change and get agreement before writing code.
- **Test-driven development (docs/plan.md §14).** Red → green → refactor for every behavior:
  - Never write production code without a failing test first. Run the test and see it fail before implementing.
  - Each stage starts with acceptance tests taken from the plan. Add each criterion to `docs/acceptance-criteria.md` and its `test_<id>_…` test in the same PR (a traceability test enforces it).
  - Bug fixes start with a test that reproduces the bug.
  - This applies to Terraform too (`terraform test`, Checkov policy checks).
- **Cost-effective by default.** Prefer options with no fixed cost: scale to zero, nothing extra per cell, shared non-request-path services. Call out the cost impact of any new infrastructure.
- **Record decisions.** New or changed decisions go in the decisions log (docs/plan.md §16).
- **Git:** never commit directly to `main`. Use a branch per change (`feat/…`, `fix/…`, `docs/…`, `infra/…`, `ci/…`) and open a PR. Commit or push only when asked.
- **PRs:** titles follow conventional commits (`feat: …`, `fix(router): …`), because squash merge makes the title the commit message. `github-actions[bot]` approves via the policy gate (docs/plan.md §9.1) when CI is green, `src/` changes include `tests/` changes, and no protected paths (`.github/**`, `scripts/ci/**`) are touched.

## Enterprise design principles (docs/plan.md §1.1)

This is a skeleton, but it must meet an **enterprise design**. It may implement less than production, never structure things differently. For every change, check:

1. **Production equivalent:** is it documented in `docs/production-architecture.md`? Local stand-ins (SQLite, Docker, `.env` tokens) must sit behind the same interface as the production service (Aurora, ECS, Secrets Manager).
2. **Boundaries:** no shortcuts across control/data plane, cells or tenants; internal APIs stay internal; one credential per role.
3. **Secure by default:** no secrets in git or logs, hardened containers, pinned images, validated input.
4. **Operable:** structured logs with `request_id` / `tenant_id` / `cell_id`, `/healthz` (alive) vs `/readyz` (dependencies OK), graceful shutdown, versioned migrations, validated and documented configuration.
5. **Stable APIs:** `/v1` paths, one error shape, cursor pagination, idempotency keys on creates.
6. **Tested like production:** acceptance criteria first, adapter tests against real Postgres/Redis/Docker.

If a change can't meet one of these, say so in the PR and record it in the decisions log.

## Architecture invariants (do not break)

1. **Control plane vs data plane.** The control plane (`src/cellarch/control_plane/`) is never on the request path. The data plane (`src/cellarch/data_plane/router/`, `src/cellarch/data_plane/cell/`) must keep serving when the control plane is down (cached routes, cells know their own tenants).
2. **One tenant lives in exactly one cell.** Routing is `tenant_id → cell`. Splitting one tenant across cells (sharding) is out of scope.
3. **Cells share nothing that can fail together.** Each cell has its own database server **and its own Kubernetes cluster**; the router and control plane run in a separate platform cluster. No cell-to-cell calls. Never put two cells in one cluster, not even as namespaces.
4. **Tenant isolation.** Each tenant has its own services and its own database on its cell's database server, with a login per service that reaches only that service's schema; queries still filter by `tenant_id` as a second line. A cell rejects tenants it doesn't host (`421`). Any change touching data access needs a cross-tenant test.
5. **All cells come from one module.** No hand-built or snowflake cells. Shared, dedicated, seasonal and customer-hosted cells differ only in inputs (`cells.yaml`).
6. **Account-agnostic infrastructure.** Terraform modules take account, region and assume-role as inputs, so a cell can be deployed into a customer's AWS account.
7. **One Terraform state file per cell.** Infrastructure changes roll out cell by cell, like code.
8. **Tenant moves use the single move procedure** (freeze → export → import → switch → delete, with rollback). Promote, demote, consolidate, seasonal and cross-account moves all reuse it.
9. **Rate limiting never takes traffic down.** The Redis-backed limiter fails open to the in-memory limiter.
10. **The router never exposes `/internal/*`** and strips internal headers from client requests.

## Stack

- Python 3.12, FastAPI, httpx, pydantic, pytest + pytest-cov (branch coverage ≥ 90% gate).
- SQLite locally, Aurora Serverless v2 PostgreSQL on AWS, behind the same storage interface.
- Runtime: Kubernetes. EKS in production (one cluster per cell + a platform cluster, Graviton, Karpenter), k3d locally. Delivery: Helm charts + Argo CD (GitOps, cell-by-cell waves).
- AWS: ALB (router only), internal NLB per cell, Aurora Serverless v2 (one cluster per cell), Secrets Manager + External Secrets, PrivateLink (customer cells), CloudWatch + OpenTelemetry. ElastiCache optional, behind a flag.
- Terraform/OpenTofu ≥ 1.10 (S3 backend, native locking). GitHub Actions with OIDC; no long-lived keys.

## Layout (planned)

```
src/cellarch/{common, control_plane, data_plane/{router,cell}, services, mgr}
web-ui/                    React + TypeScript front end (pnpm)
tests/
scripts/demo.py
infra/{bootstrap, customer-bootstrap, modules, stacks, envs}
.github/workflows/{ci,cd,cell-lifecycle,drift}.yml
charts/{platform,cell}, Dockerfile (one target per component)
docs/
```

## Commands

```powershell
. .\make up                # set up/activate .venv, load .env, k3d clusters (platform + one per cell), images, charts, status
.\make down | clean | status | build | test | setup     # macOS/Linux: make <target>
pytest                     # full in-process platform tests, no Docker needed
python scripts/demo.py     # end-to-end walkthrough against the running stack
```

`make.ps1` and `Makefile` are thin wrappers around `scripts/dev.py`; put task logic there (tested in `tests/unit/scripts/`).

## Conventions

- Components are FastAPI app factories (`create_app(settings=None, http_client=None)`) so tests can wire the whole platform in-process with httpx ASGI transports.
- Settings come from environment variables via a `from_env()` classmethod. No hard-coded endpoints or secrets.
- Time-dependent logic (rate limiter, scheduler, route cache) takes an injectable clock. Tests never sleep.
- Prefer in-memory fakes over mocks. Mock only external boundaries (AWS, GitHub).
- Every response from a cell carries `X-Cell-ID`. Every log, metric and trace carries `cell_id` and `tenant_id`.
- Headers: `X-Tenant-ID` (partition key), `X-Cell-ID`, `X-Request-ID`, `X-Internal-Token` (internal only), plus `X-RateLimit-*`.
