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

Requires Docker, plus Python 3.12 for the demo and CLI.

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1    # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

docker compose -f deploy/docker-compose.yml up --build -d --wait
python scripts/demo.py                              # onboard tenants, route, check isolation
```

**Admin CLI:** the control plane's admin API is on `localhost:8000`, with interactive API docs at `/docs`.
```powershell
$env:CELLCTL_TOKEN = "dev-admin-token"   # local development default
cellctl cells list
cellctl tenants create acme --name "Acme" --tier premium
cellctl cells set-status cell-1 draining
```

**Calling the tenant API through the router** (`localhost:8080`):
```powershell
curl -H "X-Tenant-ID: acme" -H "Content-Type: application/json" -d '{\"name\":\"first\"}' http://localhost:8080/v1/items
curl -H "X-Tenant-ID: acme" http://localhost:8080/v1/items
```

Stop and remove everything with `docker compose -f deploy/docker-compose.yml down -v`.

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
  cli/                     cellctl
tests/acceptance/          stage acceptance tests against the in-process platform
tests/unit/                unit tests per component
scripts/demo.py            end-to-end walkthrough (also the compose smoke test)
scripts/ci/pr_gate.py      PR policy gate
deploy/docker-compose.yml  local platform
Dockerfile                 one image for every component
```

## License

MIT
