# ADR 0001 — Implement root ADR 0010 (AA-Partner skeleton) in this repo

- **Status:** Proposed (23/10/2026, S248); becomes Accepted when the AA-933 PR merges.
- **Implements:** `AA-Ecosys/docs/adr/0010-aa-partner-app.md` decisions 1–3 (repo, runtime, data), ADR 0006b
  decision 6 (route classes), ADR 0003a (job table per owner) and the design card CRM-05
  (`AA-Ecosys/docs/audits/2026-10-21-S246-crm-design-v3/design-v3.md` §6.3).
- **Linear:** AA-933.

## Context

Root ADR 0010 decides the cross-repo parts: a fourth app in its own public repo, one Fargate service with its own
security group and ECR repo, schema `crm` with owner and runtime roles, contracts over HTTP only. This ADR records the
choices made **inside** AA-Partner while building the skeleton.

## Decisions

1. **Layout** `api/` now; `web/` with CRM-07 and `portal/` with CRM-P2a. Every API path is under `/crm`.
2. **Route classes enforced by one verifier and a class guard.** The byte-copied CIS verifier knows every configured
   `(issuer, client)` pair; the guard on each class router answers 403 `wrong_audience` for a verified token of another
   class and 401 for anything that does not verify. Identity values only from the environment; an unconfigured class
   accepts nothing.
3. **`tenant_tx()` is the only data path for requests**, with a static lint (no asyncpg query calls outside the DB,
   tenancy and job modules). Membership is checked by the token's `(iss, sub)` inside the RLS context.
4. **Base table template from `crm-002`** with an integration catalogue guard: base and trash columns, ENABLE + FORCE
   RLS with one policy for every role, `updated_at` trigger, status transitions table + trigger.
5. **`crm.job` runner ported from AA-Booking** with two CRM rules: the row keeps the error class only, and recurring
   kinds use one idempotency key per time slot (no singleton index in crm-001).
6. **ARM64 everywhere**: the image is built natively on GitHub's arm64 runner (CI and deploy).
7. **Health and deploy gate as in AA-Booking** (`/crm/health`, `/crm/health/ready`, `/crm/ci/deploy-gate` with the
   workflow's GitHub OIDC token), and an in-process rate limit on the public routes (CloudFront bypasses API Gateway
   throttling).

## Consequences

- A route added to a class router is guarded without extra code; a route outside the classes fails the unit test.
- Every later card copies the template and gets RLS and trash behaviour checked by CI.
- Until Infra passes the Cognito values, `/crm/v1` and the other classes refuse every token (expected on a fresh Dev).

## Alternatives considered

- **Per-class verifiers** (one `MultiIssuerVerifier` per class): simpler, but a `cis-admin` token on `/crm/v1` would
  be a 401 "unknown issuer", not the 403 the card asks for, and the logs could not tell a misrouted staff token from
  garbage.
- **Session GUC set without a transaction** (`SET` instead of `SET LOCAL`): leaks the tenant to the next request on a
  pooled connection. Rejected.
