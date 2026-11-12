# CLAUDE.md

Reference implementation of cell-based architecture for a multi-tenant SaaS platform, with automated AWS infrastructure (Terraform/OpenTofu) and CI/CD (GitHub Actions).

**Source of truth: [docs/plan.md](docs/plan.md).** Read it before making design or code changes. If a change conflicts with the plan, raise it and update the plan first. Don't silently diverge.

## Status

Planning complete. No application code yet. Work proceeds in the delivery stages in docs/plan.md §13, one stage at a time.

## Working agreements

- **Plan before code.** Propose the approach for a stage or a significant change and get agreement before writing code.
- **Test-driven development (docs/plan.md §14).** Red → green → refactor for every behavior:
  - Never write production code without a failing test first. Run the test and see it fail before implementing.
  - Each stage starts with acceptance tests taken from the plan.
  - Bug fixes start with a test that reproduces the bug.
  - This applies to Terraform too (`terraform test`, Checkov policy checks).
- **Cost-effective by default.** Prefer options with no fixed cost: scale to zero, nothing extra per cell, shared non-request-path services. Call out the cost impact of any new infrastructure.
- **Record decisions.** New or changed decisions go in the decisions log (docs/plan.md §16).
- **Git:** never commit directly to `main`. Use a branch per change (`feat/…`, `fix/…`, `docs/…`, `infra/…`, `ci/…`) and open a PR. Commit or push only when asked.
- **PRs:** titles follow conventional commits (`feat: …`, `fix(router): …`), because squash merge makes the title the commit message. `github-actions[bot]` approves via the policy gate (docs/plan.md §9.1) when CI is green, `src/` changes include `tests/` changes, and no protected paths (`.github/**`, `scripts/ci/**`) are touched.

## Architecture invariants (do not break)

1. **Control plane vs data plane.** The control plane (`src/cellarch/control_plane/`) is never on the request path. The data plane (`src/cellarch/data_plane/router/`, `src/cellarch/data_plane/cell/`) must keep serving when the control plane is down (cached routes, cells know their own tenants).
2. **One tenant lives in exactly one cell.** Routing is `tenant_id → cell`. Splitting one tenant across cells (sharding) is out of scope.
3. **Cells share nothing on the request path.** Each cell has its own database. No cell-to-cell calls.
4. **Tenant isolation.** Every query goes through the tenant-scoped data-access layer, with Postgres row-level security as a second line on AWS. A cell rejects tenants it doesn't host (`421`). Any change touching data access needs a cross-tenant test.
5. **All cells come from one module.** No hand-built or snowflake cells. Shared, dedicated, seasonal and customer-hosted cells differ only in inputs (`cells.yaml`).
6. **Account-agnostic infrastructure.** Terraform modules take account, region and assume-role as inputs, so a cell can be deployed into a customer's AWS account.
7. **One Terraform state file per cell.** Infrastructure changes roll out cell by cell, like code.
8. **Tenant moves use the single move procedure** (freeze → export → import → switch → delete, with rollback). Promote, demote, consolidate, seasonal and cross-account moves all reuse it.
9. **Rate limiting never takes traffic down.** The Redis-backed limiter fails open to the in-memory limiter.
10. **The router never exposes `/internal/*`** and strips internal headers from client requests.

## Stack

- Python 3.12, FastAPI, httpx, pydantic, pytest + pytest-cov (branch coverage ≥ 90% gate).
- SQLite locally, Aurora Serverless v2 PostgreSQL on AWS, behind the same storage interface.
- AWS: ECS Fargate (Graviton), ALB (router only), Service Connect, PrivateLink (customer cells), SSM Parameter Store, CloudWatch + OpenTelemetry. ElastiCache is optional, behind a flag.
- Terraform/OpenTofu ≥ 1.10 (S3 backend, native locking). GitHub Actions with OIDC; no long-lived keys.

## Layout (planned)

```
src/cellarch/{common, control_plane, data_plane/{router,cell}}
tests/
scripts/demo.py
infra/{bootstrap, customer-bootstrap, modules, stacks, envs}
.github/workflows/{ci,cd,cell-lifecycle,drift}.yml
deploy/docker-compose.yml, Dockerfile
docs/
```

## Commands (available once stage 1 lands)

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest                                        # full in-process platform tests, no Docker needed
docker compose -f deploy/docker-compose.yml up --build
python scripts/demo.py                        # end-to-end walkthrough against the running stack
```

## Conventions

- Components are FastAPI app factories (`create_app(settings=None, http_client=None)`) so tests can wire the whole platform in-process with httpx ASGI transports.
- Settings come from environment variables via a `from_env()` classmethod. No hard-coded endpoints or secrets.
- Time-dependent logic (rate limiter, scheduler, route cache) takes an injectable clock. Tests never sleep.
- Prefer in-memory fakes over mocks. Mock only external boundaries (AWS, GitHub).
- Every response from a cell carries `X-Cell-ID`. Every log, metric and trace carries `cell_id` and `tenant_id`.
- Headers: `X-Tenant-ID` (partition key), `X-Cell-ID`, `X-Request-ID`, `X-Internal-Token` (internal only), plus `X-RateLimit-*`.
