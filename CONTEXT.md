---
id: ARCH-06-partner
title: "AA-Partner — Repo Context"
description: Stack, boundaries, ownership, route classes, tenancy rules and working rules of AA-Partner (partner CRM API, crm.job worker, later the CRM web and the moved Content studio); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-crm-api]
last_reviewed: 2026-10-23
review_every_days: 90
related: [ARCH-01, ARCH-06-booking, docs/adr/README.md]
---

# AA-Partner — Repo Context

AA-Partner is the fourth app of the Adventure Asia ecosystem: the CRM that AA gives to partner travel agencies to run
their own B2C clients, with live AA supply inside it and privacy by architecture, plus (after the portal move) the
tenant Content studio, so an agency has one host, one sign-in and one sidebar. Today the repo holds the **skeleton**
(AA-933, design card CRM-05): API health routes, the three route classes and their token-audience guards,
`tenant_tx()` on FORCE row level security, the base table template (`crm-002`), the `crm.job` runner (heartbeat and
job-table retention), CI and the Dev deploy workflow.

> Read this file before designing or changing anything here (AA-Ecosys steering, "CONTEXT.md first"). Change it in the
> same PR when a boundary, owner, route class or the stack changes.

Design sources, in order:

1. Root ADR 0010 — AA-Partner, one app for partner agencies (`AA-Ecosys/docs/adr/0010-aa-partner-app.md`), including
   decision 9 (integrations are bring-your-own). This repo's
   [`docs/adr/0001-implements-root-0010.md`](docs/adr/0001-implements-root-0010.md) records how it is implemented here.
2. Root ADR 0006b — partner pool, MFA, staff groups and **route classes**; ADR 0006 §8 (the verifier rules).
3. Root ADR 0008 / 0008a — a new app's integration rules and the CRM's exceptions (controller / processor, own schema,
   no cross-schema grants).
4. Root ADR 0003a — one job table per owner, the shared DDL template (CIS `shared/jobs/job_table_template.sql`).
5. The design: `AA-Ecosys/docs/audits/2026-10-21-S246-crm-design-v3/` (`design-v3.md`, `final/crm-design-final.md`,
   the errata) and the spec `AA-Ecosys/docs/research/partner-crm-spec.md`.
6. `AA-Ecosys/docs/database/schema-ownership.md` — who owns which schema and version key.

## Role in the ecosystem

- **Owns** schema `crm.*` on the shared acc2 RDS (`aa-cis-dev-db`, PostgreSQL 15), migration key `crm-NNN`, owner role
  `crm_owner`, runtime role `crm_app` (NOINHERIT, connection limit 6, pool 3), and the NOLOGIN readers
  `crm_ai_reader`, `crm_insights`, `crm_support_reader`, `crm_eraser` that `crm_app` reaches only by `SET LOCAL ROLE`
  inside `tenant_tx()` (roles from AA-CIS-Infra `roles/003`, `roles/004`, AA-932).
- **Owns** the agency's data: clients (travellers), trips, proposals, the agency's own suppliers and money records,
  CRM roles (owner / agent / assistant) and the agency audit (ADR 0010 decision 5). The agency is the controller, AA
  the processor (ADR 0008a decision 1).
- **Consumes** only through versioned HTTP contracts (root `docs/contracts/`): `booking.b2b.v1` (AA-Booking),
  `catalog.http.v1`, `tenant.membership.v1` (AA-CIS-App). Service calls use the M2M pool (ADR 0006b decision 7).
- **Publishes** `crm.ops.v1` under `/crm/ops/v1` to the CIS admin (L1 platform console), later `crm.insights.v1`.
- **Never** grants another app's role anything on `crm`, never reads another app's schema (no cross-schema grants in
  either direction: the isolation probe asserts 42501 both ways), never writes content (ADR 0002), never owns prices
  (ADR 0005: Booking does).

## Stack (same as AA-Booking)

| Part | Choice |
|---|---|
| Language | Python 3.12 |
| API | FastAPI, Pydantic v2, uvicorn (keep-alive 105 s > ALB idle 100 s) |
| DB access | asyncpg with plain SQL, no ORM; `ssl="require"`; pool `max_size=3` |
| Jobs | `crm.job` (ADR 0003a template, kinds `crm_*`), worker loop inside the API process |
| Lint / tests | ruff, pytest + pytest-asyncio; integration tests on PostgreSQL 15 with the RDS role shape |
| Runtime | ECS Fargate **ARM64** service `aa-crm-dev-api` on `aa-cis-dev-cluster` (acc2, us-west-1), desired 0 until the first image |
| Edge | CloudFront `partner-dev.` -> VPC origin -> internal NLB -> ALB host + `X-AA-Origin` + `/crm/*` rule -> the service (AA-931 spike, ADR 0010 decision 2) |
| Image | ECR `aa-crm-dev-api`, IMMUTABLE, tag = git SHA, built natively on an arm64 runner |
| Frontend | `web/` (Next.js) arrives with CRM-07 (AA-935); `portal/` with CRM-P2a (AA-979) |

AWS names follow the CRM naming contract of AA-CIS-Infra `cicd_crm.tf`: everything `aa-crm-<env>-*`, tag `App = crm`
(the product is AA-Partner, the resource prefix is `crm`).

## Route classes (ADR 0006b decision 6)

| Prefix | Accepts | Then |
|---|---|---|
| `/crm/health`, `/crm/health/ready` | no token (rate limited per client address) | liveness / readiness |
| `/crm/ci/deploy-gate` | the Deploy Dev workflow's GitHub OIDC token only (`app/auth/ci.py`) | job counts for the deploy guard |
| `/crm/v1` | PARTNER x {AA-Partner web}; on Dev before AA-939 also EXTERNAL x {`cis-portal`} | `X-AA-Tenant-Id` + `tenant_tx()` (membership in `crm.member`) |
| `/crm/ops/v1` | WORKFORCE x {`cis-admin`} | group `cis-admin` reads, `crm-platform-admin` writes |
| `/crm/staff/v1` | WORKFORCE x {`crm-staff`} | group `crm-ops` or `crm-support` (+ an owner grant for support, CRM-13b) |

- One verifier (`app/auth/identity_verifier.py`, **byte copy** of AA-CIS-App `api/core/identity_verifier.py`, checked
  by CI job "Vendored CIS copies") holds every configured `(issuer, client)` pair; the class guard
  (`app/auth/route_guard.py`) then checks the pair against the class: no token or a bad token = 401, a good token of
  another class (`cis-admin`, `breakglass`, a partner token on ops, ...) = **403**. `breakglass` is accepted by no
  class. The guard sits on the router (`class_router()`), and a unit test calls every served path of each class with
  foreign tokens.
- Issuers and client ids come **only from the environment** (Terraform task definition); there are no code constants
  (public repo, DM-6.10). An unconfigured class accepts nothing. The EXTERNAL `cis-portal` bridge is refused outside
  `dev` / `test` / `local`.
- Responses carry ids and roles only, never e-mail or names from the token.

## Tenancy and data rules

- **`tenant_tx(pool, caller, tenant_id, reader_role=None)`** (`app/tenancy.py`) is the only way request code reaches
  tenant data: one transaction; `SET LOCAL app.tenant_id`; the agency must exist and be `active`; the membership check
  by the token's `(iss, sub)` (never an e-mail lookup); `SET LOCAL app.member_role` and `app.actor`; optionally
  `SET LOCAL ROLE` to one allow-listed reader. A static test fails when code outside `app/db.py`, `app/tenancy.py` and
  `app/jobs/` calls asyncpg query methods.
- **Base table template** (`api/db/templates/tenant_table.sql`, design v3 §3.2): `tenant_id`, `created_at`,
  `created_by`, `updated_at`; trash columns `deleted_at`, `deleted_by`, `purge_after` from the first migration (set
  together); ENABLE + FORCE RLS with one `tenant_isolation` policy `TO` the six CRM roles (`crm_owner`, `crm_app`,
  the four readers; never PUBLIC, ecosystem catalogue rule b); the `updated_at` trigger; a status column as
  `TEXT + CHECK` plus rows in `crm.status_transition` and its trigger. The integration test
  `test_every_crm_table_follows_the_base_template` checks every table in schema `crm` except the listed platform
  tables (`job`, `status_transition`).
- **Dev holds synthetic agencies only** (`crm.tenant.is_synthetic`, ADR 0010 decision 3); real agencies only after the
  Go-real gate (AA-980).
- Personal data never goes into `crm.job` (payloads and results are ids only, `app/jobs/payload_guard.py`, checked at
  enqueue, on every result and by a static scan of `enqueue(...)` calls), into logs (JSON renderer scrubs, ids only by
  rule) or into Sentry (`send_default_pii=False`, scrubbing).

## Migrations

| Key | File | What |
|---|---|---|
| `crm-001` | `api/migrations/001_crm_schema_job.sql` | Schema privilege model (crm_app DML defaults, no PUBLIC EXECUTE) + `crm.job` (AA-932) |
| `crm-002` | `api/migrations/002_crm_tenancy_base.sql` | `crm.tenant`, `crm.member`, `crm.status_transition`, trigger functions, `crm.v_schema_versions` (AA-933) |

Flow (data standards D8 / MIG-4): the file merges here first, then a byte copy goes to AA-CIS-Infra
`db/migrations/crm/` (Migration Copy Guard compares it with this repo's `main`), then DB Migrate `check` -> `apply`
runs it as `crm_owner`, then the `bot/db-schema-reference` PR is merged, then code that reads the new objects deploys.
`/crm/health/ready` stays 503 until every key in `app/db.py` `REQUIRED_SCHEMA_VERSIONS` is registered.

## Job runner (`crm.job`, ADR 0003a)

Ported from AA-Booking (AA-751 rules): `FOR UPDATE SKIP LOCKED` claims, guarded state changes, lease + heartbeat,
reaper (a stopped worker's job is re-claimed), release on graceful shutdown, `cancel_requested`, backoff, the backlog
sampler to CloudWatch `AA/Jobs/Crm`. CRM differences: the job row keeps the **error class only** (crm-001 column
comment); recurring kinds are scheduled with an idempotency key per time slot (`<kind>:<slot>`) because crm-001 has no
singleton index. **Only a deploy-gate / DB Migrate exempt kind may be Recurring**: a Recurring kind always holds its
next slot's row queued ahead, and both the deploy gate and the Infra DB Migrate idle check count any queued row of a
non-exempt kind as busy. Daily or slower kinds are queued **due now** by a worker maintenance hook (Booking's
`DailySchedule` pattern, AA-825), one idempotency key per UTC day.

| Kind | Every | What |
|---|---|---|
| `crm_heartbeat` | 60 s | Liveness. Always queued; exempt from the deploy gate and from the DB Migrate idle check (Infra `owners.yaml`) |
| `crm_job_retention` | daily, from `JOB_RETENTION_UTC_HOUR` (default 19 UTC), due-now hook | Data standards §7 row `crm.job`: succeeded 30 d, cancelled 90 d; **failed rows kept** until an archive bucket exists; DRY RUN (counts only) while `JOB_RETENTION_DRY_RUN` is on (default) |

## CI and release

- Required checks: **Lint**, **Security Audit** (DM-6.10 resource-ID scan, pip-audit, gitleaks over the full history
  with `.gitleaks.toml`), **Unit Tests** (coverage >= 90 %), **Integration Tests** (PostgreSQL 15, no skips),
  **Docker Build Check** (native arm64 build, container smoke, Trivy, SBOM); plus **Docs lint**, **PR title**.
  Non-required: **Vendored CIS copies**.
- Deploy Dev (`.github/workflows/deploy-dev.yml`): OIDC role `aa-crm-dev-app-deploy` (GitHub environment `dev`,
  variable `AWS_ROLE_ARN`), job guard via `/crm/ci/deploy-gate`, immutable SHA tag, latest task definition revision with
  only the image swapped, smoke on `/crm/health` + `/crm/health/ready`.
- release-please (manifest mode) keeps one release PR; the version is baked into the image and returned by
  `/crm/health`.

## Configuration (environment of the ECS task, AA-CIS-Infra `crm_app.tf`)

`DATABASE_URL` (secret `crm/dev/database-url`), `DB_SSL`, `ENVIRONMENT`, `WORKER_ENABLED`, `JOB_RETENTION_DRY_RUN`,
`JOB_RETENTION_UTC_HOUR`,
`RATE_LIMIT_PUBLIC_PER_MINUTE`, `RATE_LIMIT_XFF_HOPS`, `SENTRY_DSN` / `SENTRY_REQUIRED`, and the identity values
`COGNITO_WORKFORCE_ISSUER`, `COGNITO_CIS_ADMIN_CLIENT_ID`, `COGNITO_BREAKGLASS_CLIENT_ID`, `COGNITO_CRM_STAFF_CLIENT_ID`,
`COGNITO_PARTNER_ISSUER`, `COGNITO_PARTNER_WEB_CLIENT_ID`, `COGNITO_EXTERNAL_ISSUER`, `COGNITO_CIS_PORTAL_CLIENT_ID`.
Meanings and defaults: `api/app/settings.py`.

## Domain terms

- **Agency** (tenant): a partner travel agency, one row in `crm.tenant`. **Member**: an agency user with a CRM role
  (`owner`, `agent`, `assistant`), one row in `crm.member`, identified by the token's `(iss, sub)`.
- **Route class**: the set of `(issuer, client)` pairs one API prefix accepts.
- **Traveller / client**: the agency's own customer (later cards); never an AA Cognito user.
