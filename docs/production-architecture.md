# Production architecture

How the local skeleton becomes an enterprise production platform on AWS. The **design is the same**; production replaces local stand-ins with managed services behind the same interfaces. Status labels: **Built** (in the code today), **Planned** (stage in [plan.md](plan.md) §13), **Roadmap** (after v1).

## 1. Component mapping

| Concern | Local skeleton | Production (AWS) | Status |
|---|---|---|---|
| Entry point | Router container on `localhost:8080` | Router on **ECS Fargate (Graviton)** in **3 availability zones**, behind an **ALB** with **AWS WAF**; TLS via ACM, DNS via Route 53; autoscaled | Router built; AWS planned (stage 5) |
| Control plane | One container, SQLite | ECS service in private subnets with its own **multi-AZ Aurora PostgreSQL**; admin API only via **API Gateway + IAM**; break-glass via SSM Session Manager | Built locally; AWS planned (5) |
| Cell | Gateway + Postgres + tenant containers on a Docker network | One network boundary per cell: gateway as a multi-AZ ECS service, **its own Aurora Serverless v2 cluster**, tenant services as ECS services | Gateway: stage 2.5; AWS planned (5) |
| Tenant services | Two containers per tenant (`user-account`, `order-processing`) with CPU/memory caps | **Two ECS services per tenant**, sized by tier, autoscaled, each with **its own IAM role** | Stage 2.5; AWS planned (5) |
| Tenant data | One database per tenant on the cell's Postgres container; schema + login per service | One database per tenant in the **cell's Aurora cluster** (never shared across cells); schema + login per service; KMS encryption; point-in-time recovery | Stage 2.5; AWS planned (5) |
| Control plane data | SQLite | Aurora PostgreSQL (multi-AZ) | Planned (5) |
| Secrets | `.env`, generated compose files (git-ignored) | **Secrets Manager / SSM Parameter Store** (KMS), automatic rotation, read only by the owning IAM role | Interfaces built; AWS planned (5) |
| Service identity | Role-scoped shared tokens | **IAM / workload identity**; **mTLS** between router, gateway and services; security groups allow only expected paths | Planned (5) |
| Tenant onboarding (containers) | `cellctl` starts containers through Docker | **Deployment pipeline** (GitHub Actions + Terraform) creates the tenant's ECS services, triggered by the control plane | Stage 2.5 locally; pipeline planned (6) |
| New cell | `cellctl cells deploy` | **`cell-lifecycle.yml` + Terraform**: a new module instance = new network, gateway and **new Aurora cluster** | Local built; pipeline planned (6) |
| Shared rate limiter | Redis container (opt-in profile) | **ElastiCache (Redis/Valkey)**, multi-AZ; still fails open | Built locally; AWS planned (5) |
| Usage metering | In-memory counters, `cellctl usage` | **OpenTelemetry metrics → CloudWatch**, dashboards per cell and tenant | Counters built; export planned (5) |
| Releases | `make up` rebuilds everything | **Cell-by-cell waves**: canary cell, bake time, alarm checks, automatic rollback per wave | Planned (6) |
| Images | Built locally / in CI | Built once in CI, **scanned (Trivy)** and **signed**, stored in **ECR**, base images pinned and kept current by Dependabot | Pinning + Dependabot built; signing planned (6) |
| Customer-hosted cells | — | Cell in the customer's AWS account over **PrivateLink** | Planned (7) |

## 2. High availability

- **Every component spans at least 2 availability zones** (3 for the router): router, control plane, cell gateways, tenant services, Aurora writer + reader.
- **Static stability:** the router keeps routing from cached routes when the control plane is down (built). Cells keep serving their tenants without the control plane.
- **Blast radius, at three levels:**

  | A failure in… | …affects |
  |---|---|
  | A tenant's services | That tenant only |
  | A cell | That cell's tenants only |
  | The control plane | Onboarding and changes only; live traffic is unaffected |

- **Load shedding:**
  - **Rate limits:** per-tenant limits at the router (built), plus AWS WAF rate rules at the edge (planned).
  - **Request caps:** a per-cell cap (built) and, later, per-tenant service caps.

## 3. Scaling

- **More tenants:** add cells. Each cell brings its own compute **and its own database server**, so there is never one shared database to outgrow.
- **One busy tenant:**
  - **Autoscale** that tenant's own services.
  - **Raise its limit** (built).
  - **Move it to a dedicated cell** (stage 3).
  - **Seasonal scaling,** where it applies (stage 4).
- **Cell sizing:** fill-first placement up to a target utilization (built). A `capacity_low` warning (built) triggers adding a cell.

## 4. Backup and disaster recovery

| Scope | Mechanism | Target |
|---|---|---|
| Whole cell | Aurora continuous backup and point-in-time recovery | RPO ≤ 5 min |
| Single tenant | Restore just the tenant's database, because each tenant has its own | RPO ≤ 5 min; no impact on other tenants |
| Availability zone loss | Multi-AZ services and Aurora failover | Automatic; minutes |
| Region loss | Cross-region Aurora replicas, multi-region router and control plane | Roadmap |

Targets are design goals, to be validated with restore drills (runbook in stage 8).

## 5. Security and compliance

- **AWS Organizations:** separate accounts for dev, prod and shared services. SCPs that can't be bypassed: deny disabling CloudTrail/GuardDuty/Config, deny unapproved regions, deny root use.
- **Identity:** no long-lived keys (GitHub OIDC for CI); one IAM role per cell and per tenant service, limited to its own database and secrets; permission boundaries.
- **Network:** only the router's load balancer is public; everything else is in private subnets; no cell-to-cell traffic; WAF on the edge.
- **Data:** encryption at rest (KMS) and in transit (TLS), deletion protection, per-tenant databases. That allows **customer-managed keys per tenant** and **data residency** by choosing the cell's region.
- **Supply chain:** pinned and scanned images, Dependabot, secret scanning, branch protection with a policy gate (built), signed images (planned).
- **Detection:** CloudTrail, GuardDuty, a focused set of AWS Config rules, IAM Access Analyzer.
- **Audit:** `created_by` / `updated_by` on every record (built), a full audit log (stage 3), CloudTrail for infrastructure.
- **Compliance programmes** (SOC 2, ISO 27001): the guardrails provide the controls and evidence; certification itself is an organizational process.

## 6. Observability

- **Logs:** structured JSON with `request_id`, `tenant_id` and `cell_id` on every line, to CloudWatch Logs (7–14 day retention).
- **Traces:** OpenTelemetry across router → gateway → tenant services, to X-Ray.
- **Metrics:** request rate, errors and latency per cell and per tenant; throttles; cell request-cap rejections; database connections per tenant.
- **Dashboards and alarms:** one dashboard per cell. Alarms feed the release waves and on-call.
- **Health:** `/healthz` (alive) and `/readyz` (dependencies OK) on every component, used by load balancers and ECS.

## 7. Cost management

- **Aurora Serverless v2 auto-pause:** idle cells' databases cost close to nothing.
- **Fill-first placement:** keeps the number of cells low. Cells can be consolidated (stage 3).
- **Tenant services sized by tier.** Per-tenant containers are a deliberate, visible cost of per-tenant fault isolation.
- **Required cost tags** (`cell_id`, `tenant`, `env`), **AWS Budgets** and **Cost Anomaly Detection**.
- **No per-cell load balancer or NAT gateway;** VPC endpoints instead.

## 8. Operations

- **Infrastructure:** Terraform, with **one state file per cell**, so infrastructure changes also roll out cell by cell. Nightly drift detection.
- **Runbooks** (stage 8): cell rollout, tenant move, seasonal scaling, customer onboarding, break-glass, restore drill.
- **Operator tooling:** `cellctl` against the admin API, signed with IAM in production.

## 9. Known gaps on the way to production

| Gap | Plan |
|---|---|
| Multi-region | Roadmap: multi-region router and control plane, cross-region replicas |
| Graceful shutdown, OpenTelemetry hooks, settings reference | Cross-cutting PR after stage 2.5 |
| Versioned schema migrations, idempotency keys, cursor pagination, `/readyz` | Built into stage 2.5's tenant services |
| Signed images, release waves | Stage 6 |
| Compliance certification | Organizational process; the skeleton supplies controls and evidence |
