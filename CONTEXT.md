---
id: ARCH-06-partner
title: "AA-Partner — Repo Context"
description: Stack, boundaries, ownership, route classes, tenancy rules and working rules of AA-Partner (partner CRM API, crm.job worker, later the CRM web and the moved Content studio); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-crm-api, aa-crm-web]
last_reviewed: 2026-10-24
review_every_days: 90
related: [ARCH-01, ARCH-06-booking, docs/adr/README.md]
---

# AA-Partner — Repo Context

AA-Partner is the fourth app of the Adventure Asia ecosystem: the CRM that AA gives to partner travel agencies to run
their own B2C clients, with live AA supply inside it and privacy by architecture, plus (after the portal move) the
tenant Content studio, so an agency has one host, one sign-in and one sidebar. Today the repo holds the **walking
skeleton**: the API (AA-933, design card CRM-05: health routes, the three route classes and their token-audience
guards, `tenant_tx()` on FORCE row level security, the base table template `crm-002`, the `crm.job` runner); the
**identity vault** (AA-934, CRM-06: the `KeyProvider` with per-tenant DEKs under the KMS data key, AES-GCM field
encryption, blind indexes and the append-only audit of denials, `crm-003`); and the web (AA-935, card CRM-07: the
Next.js agency shell on the partner host with Cognito sign-in, a server session, the BFF allow-list, host routing,
EN/VI with locale dates, a health page), the agency directory `crm-004` and the Dev seeder of the synthetic agencies,
CI and the two Dev deploy workflows; and **Slice 1** (AA-936, CRM-08, `crm-005`): the public inquiry form on the
client host, `crm_intake_process` (blind-index dedupe, a repeat inquiry attaches to the open trip), the pipeline
board and trip workspace, intake-form settings, the two `crm.ops.v1` reads CIS uses, the LLM gateway stub with its
call log, and the Dev E2E; and **F1 / F2 complete** (AA-937, CRM-09, `crm-006`): `reveal(field, reason)`, the client
directory and profile, relationships with their inverse, health codes, documents under the tenant `docs` DEK, the
age band at departure, and the F2 views with the first NOLOGIN reader grants.

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
| Web | `web/`: Next.js 16 standalone (React 19, TypeScript, vitest), the AA UI kit vendored from AA-CIS-App (inline styles from the kit tokens); ECS Fargate **ARM64** service `aa-crm-dev-web` (port 3000, `/healthz`), ECR `aa-crm-dev-web`, desired 0 until the first image (AA-CIS-Infra `crm_web.tf`). `portal/` arrives with CRM-P2a (AA-979) |
| Edge for the web | the same CloudFront distribution: the ALB rule `crm_api` (`/crm`, `/crm/*`) stays above `crm_partner_default`, which forwards every other path of the partner host to the web target group |

AWS names follow the CRM naming contract of AA-CIS-Infra `cicd_crm.tf`: everything `aa-crm-<env>-*`, tag `App = crm`
(the product is AA-Partner, the resource prefix is `crm`).

## Route classes (ADR 0006b decision 6)

| Prefix | Accepts | Then |
|---|---|---|
| `/crm/health`, `/crm/health/ready` | no token (rate limited per `CloudFront-Viewer-Address`, IPv6 per /64) | liveness / readiness |
| `/crm/public/v1` | no token: the inquiry form only (AA-936), served on the **client host** too (ALB rule `/crm/public/*`) | `GET forms/{key}`, `POST forms/{key}/submissions`; the form key decides the agency inside `intake_tx()`; Origin check, honeypot, consent, per-address limit (before any read) and per-form quota (charged only after every check), body streamed and capped at 32 KiB (413) |
| `/crm/ci/deploy-gate` | the Deploy Dev workflow's GitHub OIDC token only (`app/auth/ci.py`) | job counts for the deploy guard |
| `/crm/v1` | PARTNER x {AA-Partner web}; on Dev before AA-939 also EXTERNAL x {`cis-portal`} | `X-AA-Tenant-Id` + `tenant_tx()` (membership in `crm.member`); `GET /crm/v1/memberships` alone needs no header (`caller_memberships()`) |
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
  `SET LOCAL ROLE` to one allow-listed reader. A static test (`api/tests/unit/test_code_rules.py`) allows, outside
  `app/db.py`, `app/tenancy.py` and the `app/jobs/` queue modules, only asyncpg calls on `ctx.conn` written inside
  `async with tenant_tx(...) as ctx`; a pool, `request_pool()`, `acquire()`, an alias of `ctx.conn` or a call after
  the block fails.
- **`caller_memberships(pool, caller)`** (`app/tenancy.py`, AA-935) lists the caller's agencies before any tenant is
  chosen: one transaction that sets `app.caller_iss` / `app.caller_sub` from the verified token (never
  `app.tenant_id`), read by the crm-004 SELECT-only policies `member_self_lookup` and `tenant_member_lookup` (TO
  `crm_app` only). Inside `tenant_tx()` those settings are unset, so `tenant_isolation` alone decides.
- **`provisioning_tx(pool, tenant_id, actor=...)`** is the platform context that creates an agency and its first
  members (no membership exists yet): `app.tenant_id` = the new agency, `app.actor` = `seed:*` / `platform:*`. Only
  `app/agencies.py` and the seeder may use it (`test_provisioning_tx_is_used_only_by_the_provisioning_modules`);
  `/crm/v1` request code never does.
- **`intake_tx(pool, public_key)`** (AA-936) is the public form's context: it sets `app.intake_key` from the URL key,
  read by the crm-005 SELECT-only policy `intake_form_public_lookup` (TO `crm_app`), then `app.tenant_id` = the
  form's agency (which must be active) and `app.actor` = `intake_form:<id>`. Unknown, disabled or locked = 404
  `form_not_found` (keys cannot be probed). Only `app/intake/public.py` may open it.
- **`system_tx(pool, tenant_id, actor="job:<kind>")`** (AA-936) is a job's context in one agency (no member):
  `app.tenant_id` from the job payload, the agency must be active, the same audit queue as `tenant_tx()` with
  `actor_kind = system`. Only the job handler modules (`app/intake/process.py`) and the seeder may open it.
- The DB lint also accepts `<param>.conn` in a function whose parameter is annotated with a context type
  (`TenantContext`, `SystemContext`, `ProvisioningContext`, `AnyContext`): helpers such as `app/pipeline.py` take the
  open context, they never open one.
- **Agency provisioning** (`app/agencies.py`): on `dev` / `test` / `local` an agency with `is_synthetic = false` is
  refused with **422** `synthetic_required` (card CRM-07 R test); on `prod` a synthetic one with 422
  `synthetic_not_allowed`. Names are NFC-normalised (a Vietnamese name round-trips byte for byte). Creating the same
  agency again converges (label, locale, roles), never flips `is_synthetic`, never re-activates a disabled member.
- **Base table template** (`api/db/templates/tenant_table.sql`, design v3 §3.2): `tenant_id`, `created_at`,
  `created_by`, `updated_at`; trash columns `deleted_at`, `deleted_by`, `purge_after` from the first migration (set
  together); ENABLE + FORCE RLS with one `tenant_isolation` policy `TO` the six CRM roles (`crm_owner`, `crm_app`,
  the four readers; never PUBLIC, ecosystem catalogue rule b); the `updated_at` trigger; a status column as
  `TEXT + CHECK` plus rows in `crm.status_transition` and its trigger. The integration test
  `test_every_crm_table_follows_the_base_template` checks every table in schema `crm` except the listed platform
  tables (`job`, `status_transition`).
- **Dev holds synthetic agencies only** (`crm.tenant.is_synthetic`, ADR 0010 decision 3); real agencies only after the
  Go-real gate (AA-980).
- **Field encryption** (`app/vault/`, design v3 §4.1–4.2, AA-934): one data key (DEK) per agency per purpose
  (`vault`, `docs`, `integration`, `scrub`, `ops_release`, `export`, `mail`), generated by KMS (`alias/crm-<env>/data`,
  encryption context exactly `{tenant_id, purpose}`) and stored only wrapped in `crm.tenant_key` (the Dev DEK store;
  S3 at Go-real), unwrapped DEKs cached about 5 minutes per process. Every personal field is a `*_enc` value
  `key_version ‖ nonce ‖ ciphertext ‖ tag` (AES-256-GCM, AAD = tenant ‖ table ‖ column ‖ row id); exact search and
  dedupe use `*_bidx` blind indexes (HKDF of the tenant's oldest live vault DEK, truncated HMAC). Request code uses
  `Vault(keys, ctx)` inside `tenant_tx()`; a value that does not open (another tenant's, moved, tampered) raises 403
  `decrypt_denied` and leaves an `audit_event` with outcome `denied` that survives the request's rollback.
  `KeyProvider.rotate` adds a DEK version, `rewrap` moves every DEK to another KEK without touching ciphertext (BYOK
  path), `destroy` crypto-shreds an agency. Integration secrets (`crm.integration_credential.secret_enc`) open only
  through `Vault.decrypt_secret`, allowed by lint in the dispatcher, webhook and health-check modules; no route
  response model may carry a `*_enc`, `*_bidx`, `secret*` or `wrapped_dek` field (`tests/unit/test_vault_rules.py`).
- **Audit** (`crm.audit_event`, append-only, codes and random ids only): `ctx.audit(...)` queues an event; `tenant_tx()`
  writes all of them on success and only the `denied` ones (committed) when the block raises. CRM-12 adds partitions,
  the Object Lock export and the audit page.
- Personal data never goes into `crm.job` (payloads and results are ids only, `app/jobs/payload_guard.py`, checked at
  enqueue, on every result and by a static scan of `enqueue(...)` calls), into logs (JSON renderer scrubs, ids only by
  rule) or into Sentry (`send_default_pii=False`, scrubbing).

## Migrations

| Key | File | What |
|---|---|---|
| `crm-001` | `api/migrations/001_crm_schema_job.sql` | Schema privilege model (crm_app DML defaults, no PUBLIC EXECUTE) + `crm.job` (AA-932) |
| `crm-002` | `api/migrations/002_crm_tenancy_base.sql` | `crm.tenant`, `crm.member`, `crm.status_transition`, trigger functions, `crm.v_schema_versions` (AA-933) |
| `crm-003` | `api/migrations/003_crm_vault_keys.sql` | `crm.tenant_key` (DEK store + guard trigger), `crm.integration_credential` (BYO vendor credentials, wizard health, states), `crm.traveler` (F1 vault columns + blind indexes), `crm.audit_event` (append-only) (AA-934) |
| `crm-004` | `api/migrations/004_crm_tenant_directory.sql` | `crm.tenant.display_name` + `default_locale`; SELECT-only lookup policies `member_self_lookup`, `tenant_member_lookup` TO `crm_app` (AA-935) |
| `crm-005` | `api/migrations/005_crm_intake_pipeline.sql` | Slice 1 (AA-936): `tenant_settings` (AI switches), `form_definition`, `intake_form` (+ `intake_form_public_lookup`), `form_submission` (encrypted payload), `consent` (append-only), `traveler_profile` (F2), `trip` (every stage in the CHECK, Slice 1 moves only, `crm.enforce_stage_transition`), `trip_stage_history` (append-only), `trip_traveler` / `trip_party`, `task`, `note` (encrypted), `llm_call` (append-only, no text), platform rollup `llm_usage_daily` |
| `crm-006` | `api/migrations/006_crm_f1_f2_complete.sql` | F1 / F2 complete (AA-937): `traveler_relation` (+ trigger-written inverse), `traveler_health` (needs a `health_data` consent), `document` (docs-DEK objects, write-only passports), the full F2 `traveler_profile`, `profile_history`, the age band list on `trip_party`, `audit_event.object_ids` (list views), the views `v_ai_traveler_profile` / `v_ai_trip_party` / `v_ai_trip_context` (`crm_ai_reader`) and `v_insight_fact` (`crm_insights`) |

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
| `crm_intake_process` | per stored inquiry (queued in the submission's transaction) | `app/intake/process.py`: e-mail then phone blind-index dedupe; **attach** to the lead's open trip (`inquiry` / `qualifying`, C47 in R6) or create traveller (F1) + profile (F2) + companions + trip at Inquiry + party twins + "Reply within 2 h" task + consent rows; a payload that does not open marks the submission `failed` (permanent). Not deploy-gate exempt |
| `crm_job_retention` | daily, from `JOB_RETENTION_UTC_HOUR` (default 19 UTC), due-now hook | Data standards §7 row `crm.job`: succeeded 30 d, cancelled 90 d; **failed rows kept** until an archive bucket exists; DRY RUN (counts only) while `JOB_RETENTION_DRY_RUN` is on (default) |

## CI and release

- Web checks (AA-935): **Web Build** (vitest, `next build` with the type check, standalone smoke) and **Web Docker Build
  Check** (native arm64 image, container smoke, Trivy); Security Audit also runs `npm audit` for `web/`.
- Required checks: **Lint**, **Security Audit** (DM-6.10 resource-ID scan, pip-audit), **Unit Tests** (coverage >= 90 %),
  **Integration Tests** (PostgreSQL 15, no skips), **Docker Build Check** (native arm64 build, container smoke, Trivy,
  SBOM); plus **Docs lint**, **PR title**, **gitleaks**. Non-required: **Vendored CIS copies**.
- Secret scan (`.github/workflows/secret-scan.yml`, AA-981 follow-up): **gitleaks** on every PR (lines the PR adds, rules
  and baseline read from the base branch) and **gitleaks full history** (`secret-scan-history.yml`) nightly, on push to `main` and on demand (every
  branch, tag and pull request head). Rules in `.gitleaks.toml` (same as the root repo), reviewed findings pinned in
  `.gitleaksignore`; a PR that changes either file, either secret scan workflow, `.github/CODEOWNERS` or any `.gitattributes`, or
  adds an inline `gitleaks:allow`, needs
  the label `secret-scan-baseline-reviewed` after the last push (any later push, reopen or label change removes
  it). Merge commits (remerge diff), renames and type changes are scanned too, every file as text. A red result
  means the value is public: rotate it, even if it was force-pushed away. False positives: prefer a
  narrow allowlist in `.gitleaks.toml`; a commit fingerprint does not survive the squash merge (header of
  `.gitleaksignore`).
  The PR check runs on `pull_request_target`: GitHub always runs the base branch's copy of the workflow and the PR
  head is only read as data, so a PR cannot change the check that judges it. It also scans a squash preview of the PR
  (what the squash merge writes), and it fails when another workflow has a job named `gitleaks`. `.gitleaks.toml`
  carries a copy of the gitleaks v8.30.1 default rules and no path allowlist, so every file is scanned (`.svg`,
  `go.sum`, `node_modules/`, images, lock files); UTF-16 files are transcoded to UTF-8 and scanned too.
- Deploy Dev (`.github/workflows/deploy-dev.yml`): OIDC role `aa-crm-dev-app-deploy` (GitHub environment `dev`,
  variable `AWS_ROLE_ARN`), job guard via `/crm/ci/deploy-gate`, immutable SHA tag, latest task definition revision with
  only the image swapped, smoke on `/crm/health` + `/crm/health/ready`.
- release-please (manifest mode) keeps one release PR; the version is baked into the image and returned by
  `/crm/health`.

## Web (`web/`, AA-935)

- **Hosts** (`web/lib/hosts.ts`, `web/proxy.ts`): the partner host serves the agency app; the client host
  (`WEB_CLIENT_HOSTS`, later `go-dev.`) serves `/c/*` only; any other Host is 404; `/healthz` answers any Host (the ALB
  health check sends the task IP). Cookies are host-only, so the two hosts never share a session.
- **Sign-in**: Cognito managed login, authorization code + PKCE, confidential client (on Dev the EXTERNAL `cis-portal`
  client until AA-939, design v3 Fix C10). The portal itself still signs in with its tenant API key, so there is no
  shared session until AA-979 P2b (design-final §4.4).
- **Server session** (`web/lib/session.ts`, `web/lib/seal.ts`): sealed (AES-256-GCM, HKDF key per purpose) httpOnly
  cookies `__Host-aa_crm_s` (access token, identity, agency picked) and `__Secure-aa_crm_r` (refresh token, Path
  `/auth` only); no token reaches page JavaScript. The proxy sends a page without a valid access token to
  `/auth/refresh`, which renews it or falls back to sign-in.
- **BFF allow-list** (`web/lib/bff.ts`): the browser calls `/bff/<path>` on its own origin; only listed (method, path)
  pairs are forwarded to `/crm/v1/<path>`, with the session's token and the session's agency (`X-AA-Tenant-Id` never
  comes from the browser). Cross-site writes are refused first (vendored `lib/crossSiteGuard.ts`, AA-911 rule).
  Adding a browser call = adding one line there.
- **Agency picker**: `GET /crm/v1/memberships` -> one active agency is picked automatically, several -> the chooser;
  `/auth/agency` checks the id against the caller's own list before it goes into the session.
- **EN/VI and dates** (`web/lib/i18n.ts`, `web/lib/dates.ts`, R32): date locale en-AU, en-GB, en-US or vi-VN, UI
  language from it. **English by default** (owner rule S249, AA-936): the person's own choice (cookie `aa_crm_locale`,
  the language switch) -> a language set explicitly on the page's record (only a public form's `locale` today) ->
  en-AU. The agency's `crm.tenant.default_locale` does not switch the UI and neither does Accept-Language;
  `default_locale` stays the agency's data language (the API writes trip titles in it, `app/intake/stage1.py`
  `trip_title`). Dates are always shown with the month spelled ("3 Apr 2026" en-AU, "Apr 3, 2026" en-US,
  "3 thg 4, 2026" vi-VN); typed dates follow the locale's order and the field shows the reading.
- **Shell**: the whole agency IA of design v3 §2.2 in the sidebar (each section an empty state that names its card in
  `data-card`), the Content studio cross-link (`CONTENT_STUDIO_URL`), Settings with language and dates. The AA-CIS-App
  portal sidebar links back ("Open CRM").
- **UI kit**: `web/app/_kit/*`, `web/app/_brand/tokens.ts` and `web/lib/{sameOrigin,crossSiteGuard,bffPath,proxyResponse}.ts`
  are byte copies of AA-CIS-App `frontend/` (CI "Vendored CIS copies"); `web/app/_kit/index.ts` is the CRM's barrel.
- **Configuration** (task environment, AA-CIS-Infra `crm_web.tf`; meanings in `web/lib/config.ts`): `ENVIRONMENT`,
  `WEB_PUBLIC_ORIGIN`, `WEB_PARTNER_HOSTS`, `WEB_CLIENT_HOSTS`, `CRM_API_ORIGIN`, `COGNITO_DOMAIN`, `COGNITO_CLIENT_ID`,
  `CONTENT_STUDIO_URL`, and from Secrets Manager `COGNITO_CLIENT_SECRET`, `WEB_SESSION_SECRET`. Missing identity values
  turn sign-in off (the pages say so) while `/healthz` stays up.
- **Deploy**: `.github/workflows/deploy-web-dev.yml` (same model as the API deploy, no job guard: the web runs no
  worker), gated by the `dev` environment variable `CRM_WEB_ENABLED`.

## Slice 1: intake, pipeline, LLM gateway (AA-936)

- **Inquiry form (C-01)**: `web/app/c/f/[publicKey]` on the client host renders the Stage 1 form from
  `GET /crm/public/v1/forms/{key}` (field codes from `app/intake/stage1.py`, the server's validator; the web words
  them in English unless the form definition sets `locale`, AA-936). The browser posts to `/crm/public/v1/forms/{key}/submissions` on its own origin, so the API's limits
  key on the real viewer address. The proxy's CSP `frame-ancestors` for that page = `'self'` + the form's
  `allowed_origins` (60 s memo; missing keys memoised apart, at most 20 memo misses per viewer per minute before the
  API is asked, the last good definition served when a refresh fails). Honeypot filled -> 202, nothing stored; no
  contact consent -> 422 `consent_required`, nothing stored; each drop is one count-only log line
  (`intake_dropped reason=...`).
- **Intake job** (`crm_intake_process`): dedupe by e-mail then phone blind index under per-agency advisory locks (no
  unique index on blind indexes); the consent ledger is append-only and `traveler_profile.marketing_ok` follows the
  latest marketing row. Form consent is unverified: marketing needs double opt-in before anything is sent (CRM-17 /
  CRM-29).
- **Board and trip** (`web/app/(crm)/pipeline`, API `app/pipeline.py`, router `app/routers/pipeline.py`): one
  `list_view` audit per board load (display name only, no contact data in lists, design §4.4), one `view` event per
  trip page, one `reveal` event per contact field click. Moves: `inquiry <-> qualifying`, either -> `lost` with a
  fixed reason (R23), `lost -> inquiry`; the DB enforces the edges (409 `transition_not_allowed`). Assign: the owner
  assigns anyone; others may take an unassigned trip. Settings > Forms (`web/app/(crm)/settings/forms`): hosted
  link, embed code, owner-only create / on-off / allowed origins.
- **CIS sees the CRM** (`crm.ops.v1`): `GET /crm/ops/v1/jobs/summary` (crm.job per kind) and
  `GET /crm/ops/v1/llm/usage?from&to` (the `llm_usage_daily` rollup), WORKFORCE `cis-admin` token relayed by the CIS
  API (AA-CIS-App `AppInfo("crm")`).
- **LLM gateway** (`app/llm/gateway.py`, D12): scrub receipt -> month budget (`tenant_settings.ai_budget_usd`) ->
  `ai_enabled` (default off) -> call -> one `crm.llm_call` row (no text) + the rollup; every refusal is logged too.
  Slice 1 ships the stub scrubber / provider pair (a stub scrubber is refused with any real provider); CRM-10 adds the
  scrubber, CRM-33a Bedrock.
- **Not in Slice 1** (later cards): the prospect's confirmation mail and the agent alert (traveller mail leaves the
  agency's own mailbox, CRM-17), system tags from the catalog (R29), conditional forms and the Stage 2 gate (CRM-15),
  F2 views and reader grants (CRM-09), the `form_submission` 90-day redaction job (CRM-14).
- **E2E** (`e2e/`, workflow `E2E Dev`, dispatch on main): form -> card on the board within about a minute -> trip.

## F1 / F2 complete (AA-937, CRM-09)

- **Reveal** (`app/travellers/reveal.py`): `reveal(ctx, vault, traveler_id, field, reason)` is the only agency path to
  an F1 field beyond the display name (names, e-mail, phone, DOB, nationality, passport, emergency contact, health
  codes). A reason code is required (`contact_client`, `prepare_booking`, `verify_identity`, `client_request`,
  `fill_forms`, `ops_handoff`, `other`); one `reveal` event per field in the same transaction. Every role may reveal
  (design §4.6). **No public route may reach it**: `tests/unit/test_reveal_boundary.py` walks the import graph of every
  router with a `/crm/public` or `/crm/client` prefix. A traveller sees their own record only through
  `app/travellers/self_reveal.py` `traveller_self_reveal(session)` (masked passport, own trip only; the OTP session
  is CRM-24a).
- **List views** (design §4.4): the board, the client directory (`GET /crm/v1/travelers`, exact search by e-mail /
  phone blind index or name tokens confirmed after decrypting) and a trip's document list decrypt display names (file
  names) only and write one `list_view` event per load with the ids shown (`audit_event.object_ids`). No contact data
  and no partial digits in any list; the profile shows which fields are filled, not their value.
- **Client profile** (`GET /crm/v1/travelers/{id}`, one `view` event): masked F1, consent (latest per purpose), the F2
  profile (bands and codes, reached through `traveler.profile_token_enc`), relationships, trips with the age band at
  departure, documents. Edits: F1 fields (encrypted, blind indexes in step, 409 `duplicate_traveler` on a known e-mail
  or phone), the F2 profile, health codes (`PUT .../health`, consent required, dietary / mobility mirrored to F2),
  relationships (the DB writes the inverse), the party of a trip (`trip_traveler` + `trip_party` together; a
  companion needs a name only).
- **Age band at departure** (R32): recomputed only when a DOB or a trip's start date really changes, or for the one
  new row when someone joins a trip; one `age_band_recompute` event per run (`app/travellers/age_bands.py`).
- **Documents** (`app/documents/`): the API seals each file with the tenant `docs` DEK (AAD = tenant, `document`,
  `content`, document id) before it reaches the documents bucket (`DOCUMENTS_BUCKET`, SSE-KMS as the second layer);
  key `tenants/<tenant>/documents/<document>`, SHA-256 of the stored object checked before decrypting, the file name
  encrypted, at most 7 MiB (base64 JSON through the BFF), passport scans write-only. Download is a reveal (reason
  required). Without `DOCUMENTS_BUCKET` on dev the document routes answer 503 `documents_not_configured`.
- **Readers** (design §4.3, Fix A1): `crm_ai_reader` reads `v_ai_traveler_profile`, `v_ai_trip_party`,
  `v_ai_trip_context`; `crm_insights` reads `v_insight_fact` (no token, no trip id). The views run with `crm_owner`'s
  rights under FORCE RLS (one agency, the one of `app.tenant_id`); neither role has any base table. No reader reaches
  an F1 table (`traveler`, `traveler_health`, `traveler_relation`, `document`, `trip_traveler`, `note`, `consent`,
  `form_submission`, `tenant_key`, `integration_credential`, `member`): 42501, checked by the crm-006 guard and the
  integration tests. `crm_support_reader` stays without grants until CRM-13b (AA-971). **Threat model**: the split
  protects against the readers, not against `crm_app` or the RDS master (the lead of a trip joins F2 to identity
  through `trip_id`); the DPA draft says so.

## Dev seeder (`api/app/seed/`, AA-935)

`python -m app.seed` inside the API container (ECS Exec on `aa-crm-dev-api`; it has the `crm_app` DSN): DRY RUN by
default (reads state, writes nothing), `--apply` creates or converges the three synthetic agencies of
`app/seed/catalogue.py` (fixed UUIDv5 ids, one with a Vietnamese name, default locales vi-VN / en-AU / en-US) and the
members given as `--member KEY:ROLE:SUB` (the subjects the AA-CIS-Infra e2e user script prints; issuer = partner pool
when set, else EXTERNAL). Faker EN/VI travellers (`app/seed/travellers.py`, deterministic, NFC, `.invalid` e-mail) are
generated and counted. AA-936: `--apply` makes sure each agency has the inquiry form `Website (synthetic)` (its
public key is printed); `--apply --travellers` stores the travellers as Stage 1 inquiries through
`app.intake.public.store_submission` (encrypted submission + `crm_intake_process` job, idempotent per traveller),
so they reach `crm.traveler` only through `app.vault` (needs the key provider). Refuses to run
outside `dev` / `test` / `local`. Output: one JSON line per agency, ids, labels and counts only.

## Configuration (environment of the ECS task, AA-CIS-Infra `crm_app.tf`)

`DATABASE_URL` (secret `crm/dev/database-url`), `DB_SSL`, `ENVIRONMENT`, `WORKER_ENABLED`, `JOB_RETENTION_DRY_RUN`,
`JOB_RETENTION_UTC_HOUR`,
`RATE_LIMIT_PUBLIC_PER_MINUTE`, `RATE_LIMIT_XFF_HOPS`, `SENTRY_DSN` / `SENTRY_REQUIRED`, and the identity values
`COGNITO_WORKFORCE_ISSUER`, `COGNITO_CIS_ADMIN_CLIENT_ID`, `COGNITO_BREAKGLASS_CLIENT_ID`, `COGNITO_CRM_STAFF_CLIENT_ID`,
`COGNITO_PARTNER_ISSUER`, `COGNITO_PARTNER_WEB_CLIENT_ID`, `COGNITO_EXTERNAL_ISSUER`, `COGNITO_CIS_PORTAL_CLIENT_ID`,
and the key provider `KEY_PROVIDER` (`kms`; `local` only in test / local), `KMS_DATA_KEY_ID` (`alias/crm-<env>/data`),
`DEK_CACHE_TTL_SECONDS`, and the public intake (AA-936) `INTAKE_PAGE_ORIGINS` (the client host origin; empty on Dev =
every submission 503 `intake_not_configured`), `RATE_LIMIT_INTAKE_PER_MINUTE` (default 5 per address),
`RATE_LIMIT_INTAKE_FORM_PER_HOUR` (default 200 per form), and the documents bucket (AA-937) `DOCUMENTS_BUCKET`. Meanings and defaults: `api/app/settings.py`. Live check of the key provider inside the
container: `python -m app.vault.selftest` (KMS round trip under the nil tenant context, nothing stored).

## Domain terms

- **Agency** (tenant): a partner travel agency, one row in `crm.tenant`. **Member**: an agency user with a CRM role
  (`owner`, `agent`, `assistant`), one row in `crm.member`, identified by the token's `(iss, sub)`.
- **Route class**: the set of `(issuer, client)` pairs one API prefix accepts.
- **Traveller / client**: the agency's own customer, one row in `crm.traveler` (F1 vault, every identity field
  encrypted); never an AA Cognito user. Its **F2 profile** (`crm.traveler_profile`, random `traveler_token`, bands
  and codes only) is reached only through the encrypted pointer `traveler.profile_token_enc`.
- **Reveal**: opening one F1 field of one traveller on a member's click, with a reason code; always audited.
- **DEK / KEK**: a tenant data key (one per agency per purpose and version) and the key that wraps it (the KMS data
  key, or an agency's own key under BYOK later). **Blind index**: a keyed, truncated hash of a normalised value for
  exact match without decrypting.
- **Synthetic agency**: a Dev agency with `is_synthetic = true` and invented data (`app/seed/`); the only kind Dev
  accepts (ADR 0010 decision 3).
- **Inquiry**: a trip at stage `inquiry`, created by `crm_intake_process` from a form submission; a repeat
  submission of the same e-mail (or phone) while the lead's trip is still `inquiry` / `qualifying` attaches to it.
- **Intake form**: one public form of an agency (`crm.intake_form`, random `public_key` in its URL); its answers are a
  **submission** (`crm.form_submission`, payload encrypted, redacted after 90 days).
- **Partner host / client host**: the agency app's host (Dev `partner-dev.`) and the travellers' host (later
  `go-dev.`); never one session across both.
