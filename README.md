# AA-Partner

Partner CRM of the Adventure Asia ecosystem: one app for partner travel agencies to run their own B2C clients
(pipeline, proposals with live AA supply, payments, messages), with privacy by architecture, and — after the portal
move — the tenant Content studio. Root ADR 0010 (`AA-Ecosys/docs/adr/0010-aa-partner-app.md`) is the placement
decision; the product spec and design live in the AA-Ecosys root repo.

**Status:** walking skeleton. API (AA-933, card CRM-05): health routes, the three route classes and their
token-audience guards, `tenant_tx()` on FORCE RLS, the base table template (`crm-002`), the `crm.job` runner. Web
(AA-935, card CRM-07): the Next.js agency shell on the partner host (Cognito sign-in with a server session, the agency
picker, the sidebar of the whole information architecture with empty states, EN/VI with locale dates, health page),
the BFF allow-list, host routing for the client host, and the Dev seeder of the synthetic agencies (`crm-004`). No
agency feature yet. Start with [CONTEXT.md](CONTEXT.md).

## Layout

| Path | What |
|---|---|
| `api/` | FastAPI + asyncpg API with the in-process `crm.job` worker (Python 3.12, ARM64 image) |
| `api/migrations/` | `crm-NNN` migrations (byte copies in AA-CIS-Infra `db/migrations/crm/`, applied by DB Migrate) |
| `api/db/templates/` | The base table template every tenant table copies |
| `web/` | CRM UI and the client host (Next.js 16 standalone, ARM64 image; AA-935) |
| `api/app/seed/` | Dev seeder of the synthetic agencies (`python -m app.seed`, DRY RUN by default; AA-935) |
| `portal/` | Tenant Content studio (Next.js, Vercel `aa-cis-portal`), moved from AA-CIS-App with its history by CRM-P2a (AA-979); CONTEXT.md "Tenant portal" |
| `e2e/` | Playwright: the CRM Dev E2E (`playwright.config.ts`) and the portal UI smoke (`playwright.portal.config.ts`) |
| `docs/` | Repo ADRs, implementation notes |
| `tools/docs/` | Docs lint tools (byte copies of the root repo's) |

## Endpoints (Dev host `partner-dev.lumiguides.it.com`)

| Path | Who | Purpose |
|---|---|---|
| `GET /crm/health` | public, rate limited | Liveness, no DB. ALB health check. `{status, service, version, git_sha}` |
| `GET /crm/health/ready` | public, rate limited | DB reachable + every required `crm-NNN` registered. 200 or 503 |
| `GET /crm/ci/deploy-gate` | Deploy Dev workflow OIDC token only | Queued/running `crm.job` rows a deploy would interrupt |
| `GET /crm/v1/me` | agency (partner pool; Dev: EXTERNAL `cis-portal`) + `X-AA-Tenant-Id` | The caller's membership: `{tenant_id, member_id, member_role, display_name, default_locale}` |
| `GET /crm/v1/memberships` | agency token, no tenant header | The caller's agencies: `{memberships: [{tenant_id, member_id, member_role, display_name, default_locale, tenant_status}]}` |
| `/crm/ops/v1/*` | WORKFORCE `cis-admin` | `crm.ops.v1` contract (CRM-13a) — guard only today |
| `/crm/staff/v1/*` | WORKFORCE `crm-staff`, groups `crm-ops` / `crm-support` | Ops release, support sessions (CRM-26, CRM-13b) — guard only today |

Web (same host, every path that is not `/crm` or `/crm/*`): `/healthz` (liveness JSON, ALB health check), `/health`
(page: web build + API liveness and readiness), `/auth/sign-in`, `/auth/callback`, `/auth/refresh`, `/auth/sign-out`,
`/auth/agency`, `/prefs/locale`, `/bff/<path>` (the allow-listed proxy to `/crm/v1`), the agency pages, `/c/*` on the
client host only.

## Run the tests

```bash
cd api
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
ENVIRONMENT=test .venv/bin/pytest tests/unit -q
# Integration: a throwaway PostgreSQL 15 database whose name ends in _test or _ci
export CRM_TEST_ADMIN_DSN="postgresql://aa_cis_admin:<pw>@localhost:5432/crm_test" ENVIRONMENT=test
.venv/bin/python -m tests.integration.bootstrap_roles "postgresql://postgres:<pw>@localhost:5432/crm_test"
.venv/bin/pytest tests/integration -q
```

Web (Node 22; never run `npm install` in the main checkout of the workspace, use a worktree):

```bash
cd web
npm ci
npm test
NODE_OPTIONS=--max-old-space-size=1536 npm run build
```
