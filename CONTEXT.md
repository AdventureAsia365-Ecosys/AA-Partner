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
age band at departure, and the F2 views with the first NOLOGIN reader grants; and **F5 audit complete** (AA-940,
CRM-12, `crm-007`): monthly audit partitions with a verified 12-month drop, the hourly export to both Object Lock
buckets with a SHA-256 manifest chain, its verify script, and the owner's access log page with download; and **F4 roles**
(AA-939, CRM-11, `crm-008`): the permission matrix, the margin serializer on every `/crm/v1` response, the partner pool
sign-in with required MFA, invitations, team management, the reassign queue, owner recovery and the plan columns; and the
**F3 PII scrubber** (AA-938, CRM-10, `crm-009`): known values -> regex -> Bedrock Guardrails before any LLM call, fail
closed, the encrypted placeholder map and the EN/VI corpus gate; and **data lifecycle** (AA-942, CRM-14, `crm-010`): one trash with the agency's window, the delete block, archive enforced at the
dispatcher, legal holds, client erasure and agency offboarding through one deletion function (`crm.erase_subject`),
the daily `crm_lifecycle` run in DRY RUN by default, and Settings > Data and privacy; and the **L1 console**
(AA-941, CRM-13a, `crm-011`): `/crm/ops/v1` for the CIS admin "Agencies (CRM)" page (agency list, usage meters,
create agency, lock / unlock, first-owner invite, allowed features, insights opt-out, AA-raised export / deletion
requests the owner confirms, failed-job retry, owner recovery) behind a response key allow-list.

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
| `/crm/public/v1/invitations/claim` | no token (AA-939): the invitation token in the body only; rate limited per address (`RATE_LIMIT_CLAIM_PER_MINUTE`) | `invitation_tx()`; creates or finds the partner-pool account |
| `/crm/v1` | PARTNER x {AA-Partner web}; on Dev also EXTERNAL x {`cis-portal`} until the CRM web switches pools (AA-CIS-Infra `crm_partner_signin_enabled`; removed at P2b, AA-979) | `X-AA-Tenant-Id` + `tenant_tx(action=...)` (membership in `crm.member`, then the permission matrix); `GET /crm/v1/memberships` and `POST /crm/v1/invitations/accept` need no header |
| `/crm/ops/v1` | WORKFORCE x {`cis-admin`} | group `cis-admin` or `crm-platform-admin` reads, `crm-platform-admin` writes (AA-939: member deactivation, owner recovery; AA-941: the L1 console, section "L1 console" below). Every response passes the L1 key allow-list (`app/ops/serializer.py`) |
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
- **`tenant_tx(..., action=Action.X)`** (AA-939): right after the membership check the permission matrix
  (`app/permissions.py`) refuses a role whose rule is DENY: 403 `role_forbidden` and a committed `denied` event. Every
  `tenant_tx(` call in `app/routers/` must pass `action=` (`test_code_rules.py`). It also records the member role for
  the margin serializer (`app/margin.py`).
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
- **`invitation_tx(pool, token_hash_hex)`** (AA-939) is the invitation claim / accept context: it sets
  `app.invite_token_hash`, read by the crm-008 SELECT-only policy `invitation_token_lookup` (TO `crm_app`); the
  invitation found decides the agency (must be active), `app.actor` = `invitation:<id>` (audit actor kind `user`).
  Unknown token or locked agency = 404 `invitation_not_found`. The row is then re-read `FOR UPDATE`, so two claims
  or two accepts of one invitation run one after the other. Only `app/members/invitations.py` may open it.
- **`system_tx(pool, tenant_id, actor="job:<kind>")`** (AA-936) is a job's context in one agency (no member):
  `app.tenant_id` from the job payload, the agency must be active, the same audit queue as `tenant_tx()` with
  `actor_kind = system`. Actors `job:` / `seed:` only. Only the job handler modules (`app/intake/process.py`) and the
  seeder may open it.
- **`platform_tx(pool, tenant_id, actor="platform:<staff sub>", statuses={"active"})`** (AA-939, AA-941) is AA staff
  in one agency (L1 console, `/crm/ops/v1`, audit kind `aa_ops`): the crm-008 guards let this actor remove the last
  owner, open / complete an owner recovery and write entitlements. `statuses` = the agency statuses the call may meet
  (default active only; reads all four; lock / unlock and entitlements active + locked; a job retry active +
  offboarding); a missing agency is 404 `tenant_not_found`, a refused status 409 `tenant_locked` / `tenant_status`.
  Only `app/members/recovery.py` and `app/ops/console.py` may open it, and only `app/routers/ops.py` (writes need the
  WORKFORCE group `crm-platform-admin`) calls their platform functions; the helpers a platform context is handed
  (`offboarding.raise_request`, `team.create_invitation`, `queue.retry_failed`) are called only from their module
  and the console (code-rules tests).
- **crm-008 membership rules** (DB, whatever the API code does): inside a member request only an owner writes
  `crm.member` / `crm.member_invitation` (CRM02); a member's identity (`tenant_id`, `cognito_iss`, `cognito_sub`) is
  fixed outside platform contexts; no crm_app session deletes a member; an invitation moves only itself, pending ->
  claimed -> accepted (or expires); the last active owner stays unless a platform context acts (CRM01); a crm_app
  session without `app.actor` writes none of these tables.
- **`lifecycle_tx(pool, tenant_id, actor=..., statuses=...)`** (AA-942) is a lifecycle job's context in one agency
  (`job:crm_lifecycle`, `job:crm_offboard`): the audit queue of `system_tx()`, and the agency statuses the caller names
  (default active / locked: a locked agency still has retention; the erase step opens an `offboarding` one). Only
  `app/lifecycle/{job,offboarding}.py` may open it (`test_lifecycle_tx_is_used_only_by_the_lifecycle_jobs`).
  **`as_eraser(ctx)`** switches an open context to `crm_eraser` for one call of the crm-010 deletion functions inside a
  savepoint (a failed call rolls the role back too); only `app/lifecycle/erase.py` calls those functions
  (`test_only_the_erase_module_calls_the_deletion_functions`).
- The DB lint also accepts `<param>.conn` in a function whose parameter is annotated with a context type
  (`TenantContext`, `SystemContext`, `ProvisioningContext`, `AnyContext`, `LifecycleContext`): helpers such as `app/pipeline.py` take the
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
  writes all of them on success and only the `denied` ones (committed) when the block raises. Partitions, export,
  retention and the access log: section "F5 audit" below (AA-940).
- **`audit_export_tx(pool, tenant_id)`** (AA-940) is the audit export's read context in one agency: `app.tenant_id`,
  `app.actor` = `job:crm_audit_export`, no audit queue; unlike `system_tx()` it accepts a locked or trashed agency (an
  offboarded agency is exported until its partitions drop). Only `app/audit_export/{export,verify,retention}.py` may
  open it (`test_audit_export_tx_is_used_only_by_the_audit_export_jobs`); the crm-007 platform functions (agency list,
  partition maker, drop) are called from `app/audit_export/registry.py` only.
- Personal data never goes into `crm.job` (payloads and results are ids only, `app/jobs/payload_guard.py`, checked at
  enqueue, on every result and by a static scan of `enqueue(...)` calls), into logs (JSON renderer scrubs, with the F3
  pass-2 detectors since AA-938, ids only by rule), into Sentry (`send_default_pii=False`, scrubbing) or into an LLM
  prompt unscrubbed (F3, AA-938).

## Migrations

| Key | File | What |
|---|---|---|
| `crm-001` | `api/migrations/001_crm_schema_job.sql` | Schema privilege model (crm_app DML defaults, no PUBLIC EXECUTE) + `crm.job` (AA-932) |
| `crm-002` | `api/migrations/002_crm_tenancy_base.sql` | `crm.tenant`, `crm.member`, `crm.status_transition`, trigger functions, `crm.v_schema_versions` (AA-933) |
| `crm-003` | `api/migrations/003_crm_vault_keys.sql` | `crm.tenant_key` (DEK store + guard trigger), `crm.integration_credential` (BYO vendor credentials, wizard health, states), `crm.traveler` (F1 vault columns + blind indexes), `crm.audit_event` (append-only) (AA-934) |
| `crm-004` | `api/migrations/004_crm_tenant_directory.sql` | `crm.tenant.display_name` + `default_locale`; SELECT-only lookup policies `member_self_lookup`, `tenant_member_lookup` TO `crm_app` (AA-935) |
| `crm-005` | `api/migrations/005_crm_intake_pipeline.sql` | Slice 1 (AA-936): `tenant_settings` (AI switches), `form_definition`, `intake_form` (+ `intake_form_public_lookup`), `form_submission` (encrypted payload), `consent` (append-only), `traveler_profile` (F2), `trip` (every stage in the CHECK, Slice 1 moves only, `crm.enforce_stage_transition`), `trip_stage_history` (append-only), `trip_traveler` / `trip_party`, `task`, `note` (encrypted), `llm_call` (append-only, no text), platform rollup `llm_usage_daily` |
| `crm-006` | `api/migrations/006_crm_f1_f2_complete.sql` | F1 / F2 complete (AA-937): `traveler_relation` (+ trigger-written inverse), `traveler_health` (needs a `health_data` consent), `document` (docs-DEK objects, write-only passports), the full F2 `traveler_profile`, `profile_history`, the age band list on `trip_party`, `audit_event.object_ids` (list views), the views `v_ai_traveler_profile` / `v_ai_trip_party` / `v_ai_trip_context` (`crm_ai_reader`) and `v_insight_fact` (`crm_insights`) |
| `crm-007` | `api/migrations/007_crm_audit_partitions_export.sql` | F5 audit (AA-940): `audit_event` PARTITION BY RANGE (`at`), the crm-003 table attached as `audit_event_legacy` (MINVALUE to the next month), `at` = insert time (explicit values only from `crm_owner`), `audit_partition_bounds()`, `audit_ensure_partitions()` (three months ahead), `audit_drop_expired_partitions(dry_run)` (12 months + every agency verified), `audit_export_tenant` (registry, RLS enabled not forced, `audit_export_agencies()`), `audit_export_manifest` (hash chain, trigger-enforced, no FK to `tenant`) |
| `crm-008` | `api/migrations/008_crm_roles_members.sql` | F4 roles (AA-939): `crm.guard_member_write` (only an owner writes memberships inside a member request, CRM02; the last active owner stays unless a platform context acts, CRM01), `member_invitation` (token hash, encrypted e-mail + blind index, claimed `(iss, sub)`; `invitation_token_lookup`), `owner_recovery_request` (14 days, platform-only open / complete, CRM03 / CRM04), `tenant_entitlement` (plan, member limit, features; platform-only), `tenant_settings.refund_owner_threshold_base`, `UNIQUE (tenant_id, member_id)` on `member` |
| `crm-009` | `api/migrations/009_crm_scrub_placeholder_map.sql` | F3 scrubber (AA-938): `placeholder_map` (scrub DEK, written once, 24 h CHECK, same-agency key to its `llm_call`, no reader grant; DELETE-only purge policy `placeholder_map_expired_purge` keyed on `app.purge_scope`), `llm_call.scrub_receipt` closed at every level (receipt keys, version codes, numbers, `counts` / `passes` keys), `UNIQUE (tenant_id, llm_call_id)`. Needs crm-006 only; independent of crm-007 / crm-008 |
| `crm-010` | `api/migrations/010_crm_data_lifecycle.sql` | Data lifecycle (AA-942; needs crm-007 only, independent of crm-008 / crm-009): agency statuses `offboarding` / `offboarded`, `tenant_settings.trash_days` (1-30, default 7) and `auto_archive_months` (12-120, default 36), `traveler.archived_by` + archive reason codes, `legal_hold` (platform holds released by platform only), `trip_commitment` + `trip_delete_blockers()` (the delete-block ledger), `dispatch_queue`, `erasure_request`, `lifecycle_run` (append-only), `erasable_append_only()` on consent / trip_stage_history / llm_call, `legal_hold_blocks()`, `erase_subject()` and `lifecycle_purge_expired()` (SECURITY DEFINER, EXECUTE for `crm_eraser` only) |
| `crm-011` | `api/migrations/011_crm_l1_console.sql` | L1 console (AA-941; needs crm-008 + crm-010): `tenant_entitlement.insights_opt_out` (Q2 3.12, platform-only through the crm-008 trigger), `erasure_request.purpose` (`offboard` / `export_only`) + `owner_accepted_at`, `crm.guard_erasure_request_owner_confirm()` (CRM18: AA raises an agency request, only the owner confirms it, once; an unconfirmed AA request only gets cancelled; export_only never erases; AA never confirms or runs one). No new table |
| `crm-012` | `api/migrations/012_crm_modules_1_2.sql` | CRM-15 Modules 1 + 2 (AA-943; needs crm-011): every trip stage edge (lost before booked, cancelled from booked / pre_departure / travelling, the date-rule edges); `crm.enforce_stage_transition()` gates (qualifying -> proposal: `intake_completeness.s2` >= 80 or `app.gate_override = stage2`; proposal -> booked `booking`; booked -> pre_departure `stage3`; CRM19) and Undo (`app.stage_undo` = the trip's latest move, <= 15 s, same actor, not a cancel; CRM20); `trip.intake_completeness` / `cancel_reason_code` / `last_activity_at`; `trip_stage_history.override_reason_code` / `undo_of` / `batch_id` (at and actor pinned on insert); `task.priority`; `tenant_settings.timezone` / `stage2_auto_send` / `call_slot_rules`; `client_link` (signed links, `client_link_token_lookup` on `app.client_link_hash`) and `meeting_slot` (R15); Stage 2 / 3 `form_submission` rows (`client_link_id`, outcome `applied`); `crm.erase_subject` / `crm.erase_held` / hold guards cover the two new tables |
| `crm-013` | `api/migrations/013_crm_membership_feed.sql` | CIS membership feed consumer (AA-978; needs crm-012): `crm.tenant.lock_source` (`aa_ops` / `cis_tenant` / `cis_membership`, set only while locked; `crm.sync_tenant_lock_source()`: leaving locked clears it, a lock naming no source is `aa_ops`, only the actor `platform:feed-tenant-membership` names a `cis_*` source, CRM21), `crm.membership_feed_state` (platform singleton: cursor, last success, last error class, failures), `crm.membership_inbox` (one append-only row per CIS event, `event_id` key, outcome + reason code, FORCE RLS per agency, erased with the agency); `crm.erase_subject` = the crm-012 text + the inbox in the agency scope |

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
| `crm_audit_export` | hourly, queued due now from minute 16 (`HourlyAuditExportSchedule`) | AA-940: keeps the audit partitions three months ahead, then exports every agency's closed hours (15-minute lag, at most 48 per agency per run) to both Object Lock buckets and the manifest chain; `not_configured` without the buckets. Not deploy-gate exempt |
| `crm_audit_retention` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook | AA-940: verifies every agency from `verified_until` (rows, chain, both copies), moves `verified_until`, then drops expired partitions (dry run while `AUDIT_RETENTION_DRY_RUN` is on, the default) |
| `crm_placeholder_purge` | hourly, due-now hook (`app.privacy.purge.HourlyPlaceholderPurge`, key per UTC hour) | AA-938: deletes expired F3 placeholder maps of every agency in one DELETE through the crm-009 purge policy (no read); count only. Not deploy-gate exempt (exists only while due / running) |
| `crm_lifecycle` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook (`DailyLifecycleSchedule`) | AA-942: per agency (active / locked): form-submission payloads redacted after 90 days, LLM calls older than 180 days purged (with their crm-009 placeholder maps), trash past its window purged through `crm.erase_subject`, inactive clients auto-archived; one `lifecycle_run` row + one audit event per agency; skips anything under a legal hold or the delete block; DRY RUN (counts only) while `LIFECYCLE_DRY_RUN` is on (default). Not deploy-gate exempt |
| `crm_offboard` | per owner step (queued in the request's transaction) | AA-942: `mode=export` builds and seals the agency export; `mode=erase` erases the agency, deletes its objects, destroys its keys, writes the receipt. Not deploy-gate exempt |
| `crm_dispatch` | per queued message (after the 10 s undo delay for a user action) | AA-942: the dispatcher gate at send time (trashed, archived, no marketing consent -> dropped; no sender -> held). Not deploy-gate exempt |
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
- **Sign-in**: Cognito managed login, authorization code + PKCE, confidential client: the partner pool's `partner-web`
  client (MFA required) once AA-CIS-Infra `crm_partner_signin_enabled` is on, the EXTERNAL `cis-portal` client before
  (design v3 Fix C10). The web reads the domain and client from its environment only. The portal itself still signs in with its tenant API key, so there is no
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
  `ai_enabled` (default off) -> seal the placeholder map -> call -> one `crm.llm_call` row (no text) + the rollup ->
  store the map; every refusal is logged too and every non-`ok` result carries `fallback = manual_reply_needed`.
  Slice 1 shipped the stub scrubber / provider pair (a stub scrubber is refused with any real provider); AA-938 adds
  the F3 scrubber (below), CRM-33a Bedrock.
- **Not in Slice 1** (later cards): the prospect's confirmation mail and the agent alert (traveller mail leaves the
  agency's own mailbox, CRM-17), system tags from the catalog (R29), conditional forms and the Stage 2 gate (CRM-15),
  F2 views and reader grants (CRM-09), the `form_submission` 90-day redaction job (CRM-14, done in AA-942).
- **E2E** (`e2e/`, workflow `E2E Dev`, dispatch on main): form -> card on the board within about a minute -> trip.

## F1 / F2 complete (AA-937, CRM-09)

- **Reveal** (`app/travellers/reveal.py`): `reveal(ctx, vault, traveler_id, field, reason)` is the only agency path to
  an F1 field beyond the display name (names, e-mail, phone, DOB, nationality, passport, emergency contact, health
  codes). A reason code is required (`contact_client`, `prepare_booking`, `verify_identity`, `client_request`,
  `fill_forms`, `ops_handoff`, `other`); one `reveal` event per field in the same transaction. Every role may reveal
  (design §4.6). Health codes come back only while the traveller's latest `health_data` consent is true (else 409
  `health_consent_withdrawn` and a denied event). **No public route may reach it**: `tests/unit/test_reveal_boundary.py`
  walks the import graph of every router under `/crm/public` or `/crm/client` (found by `APIRouter(prefix=)`, by
  `class_router(<RouteClass>)` and by the live routes). **Reveals per member per hour** (design §4.4):
  `app/observability/reveal_meter.py` counts committed allowed reveals (fields and document downloads) per agency and
  member over a sliding hour; the job metrics sampler publishes the peak as `AA/Jobs/Crm` `RevealsPerMemberPeakHour`
  (Infra alarm `aa-crm-<env>-bulk-reveal`), and 60 in an hour logs `crm_bulk_reveal`. A traveller sees their own record only through
  `app/travellers/self_reveal.py` `traveller_self_reveal(session)` (masked passport, own trip only; the OTP session
  is CRM-24a).
- **List views** (design §4.4): the board, the client directory (`GET /crm/v1/travelers`; exact search by e-mail /
  phone blind index or name tokens confirmed after decrypting is `POST /crm/v1/travelers/search`, the term in the body,
  and `GET ?q=` answers 422 `search_by_post`; the web keeps the term in a sealed 15-minute cookie on `/clients`, never
  in a URL) and a trip's document list decrypt display names (file
  names) only and write one `list_view` event per load with the ids shown (`audit_event.object_ids`). No contact data
  and no partial digits in any list; the profile shows which fields are filled, not their value.
- **Client profile** (`GET /crm/v1/travelers/{id}`, one `view` event naming the related travellers and documents
  shown in `object_ids`; the trip page's `view` names its party the same way): masked F1, consent (latest per purpose), the F2
  profile (bands and codes, reached through `traveler.profile_token_enc`), relationships, trips with the age band at
  departure, documents. Edits: F1 fields (encrypted, blind indexes in step, 409 `duplicate_traveler` on a known e-mail
  or phone), the F2 profile, health codes (`PUT .../health`, consent required, dietary / mobility mirrored to F2),
  relationships (the DB writes the inverse; an explicit referrer relation overwrites the intake's
  `referrer_traveler_id`), the party of a trip (`trip_traveler` + `trip_party` together; a
  companion needs a name only).
- **Age band at departure** (R32): recomputed only when a DOB or a trip's start date really changes, or for the one
  new row when someone joins a trip; one `age_band_recompute` event per run (`app/travellers/age_bands.py`).
- **Documents** (`app/documents/`): the API seals each file with the tenant `docs` DEK (AAD = tenant, `document`,
  `content`, document id) before it reaches the documents bucket (`DOCUMENTS_BUCKET`, SSE-KMS as the second layer);
  key `tenants/<tenant>/documents/<document>`, SHA-256 of the stored object checked before decrypting, the file name
  encrypted, at most 7 MiB (base64 JSON through the BFF), passport scans write-only. Download is a reveal (reason
  required). Without `DOCUMENTS_BUCKET` on dev the document routes answer 503 `documents_not_configured`; a missing
  object (S3 `NoSuchKey`, or `AccessDenied` since the role has no `ListBucket`) answers 503 `document_missing`.
- **Same-agency keys** (crm-006): every new reference to a traveller, trip or profile is a `(tenant_id, id)` foreign
  key (referential checks bypass FORCE RLS), and `document.s3_key` must equal `tenants/<its tenant>/documents/<its id>`.
  The crm-003 / crm-005 single-column references (`trip_traveler`, `trip_party`, ...) get the same in a later file.
- **Readers** (design §4.3, Fix A1): `crm_ai_reader` reads `v_ai_traveler_profile`, `v_ai_trip_party`,
  `v_ai_trip_context`; `crm_insights` reads `v_insight_fact` (no token, no trip id). The views run with `crm_owner`'s
  rights under FORCE RLS (one agency, the one of `app.tenant_id`); neither role has any base table. No reader reaches
  an F1 table (`traveler`, `traveler_health`, `traveler_relation`, `document`, `trip_traveler`, `note`, `consent`,
  `form_submission`, `tenant_key`, `integration_credential`, `member`): 42501, checked by the crm-006 guard and the
  integration tests. `crm_support_reader` stays without grants until CRM-13b (AA-971). `v_insight_fact` shows one
  agency at a time (FORCE RLS on `app.tenant_id`), so CRM-34's cross-agency aggregate (n >= 10, >= 3 agencies) needs
  its own path (for example a `SECURITY DEFINER` aggregate owned by `crm_owner`), not this view. **Threat model**: the split
  protects against the readers, not against `crm_app` or the RDS master (the lead of a trip joins F2 to identity
  through `trip_id`); the DPA draft says so.

## F5 audit (AA-940, CRM-12)

- **What is logged** (design §4.9): sign-ins (`POST /crm/v1/audit/sign-in`, sent server side by the web's
  `/auth/agency`: a GET auto-pick only right after a Cognito sign-in (one-shot `si` session flag), a chooser POST
  always; not on the browser BFF list), role changes (`provision_agency`: `agency_create`, `member_add`,
  `member_role_change`, actor `aa_ops` for `platform:` / `system` for `seed:`), one `audit_retention` event per run per
  active agency (the daily job; the hourly export's per-run record is its manifest row), every F1 reveal (same transaction), list views, views, creates / changes / deletes, refused owner-only
  actions (`denied`; since AA-939 the permission matrix's action and reason, e.g. `reassign` / `role_forbidden`), decrypt denials, key operations (`key_create`,
  `key_rotate`, `key_rewrap`, `key_destroy`, `app/vault/provider.py`), AI context builds (`ai_context_build`, actor
  `ai_agent` / `ai:<stage>`, one per LLM gateway call), intake job events, the access log's own views and downloads.
  Later cards add their actors (L1 writes, support reads, break-glass Decrypts) with the same table.
- **Partitions and retention** (crm-007): one partition per UTC month; the hourly job keeps three months ahead (no
  DEFAULT partition: an insert with no partition fails, which fails the request; alarm on `AuditPartitionMonthsAhead`
  < 1). A partition is dropped only when it ends at or before the start of the UTC month twelve months ago **and**
  every agency is verified past it; `DROP TABLE` fires no row trigger, so every kept row stays append-only.
  `verified_until` moves only through `crm.audit_mark_verified()` (forward, never past `exported_until`; `crm_app`
  updates only `exported_until` / `last_seq`). Accepted risk: the database cannot re-read the buckets, so a
  compromised `crm_app` could still mark and drop expired partitions; the Object Lock archive stays.
  The partition maker runs with `lock_timeout` 2 s; the crm-api pool ends a transaction idle for 2 minutes (far below
  the export's 15-minute read lag).
- **Export** (`app/audit_export/export.py`, `chain.py`): per agency per closed UTC hour one object
  `audit/v1/tenants/<tenant>/<yyyy>/<mm>/<dd>/<hh>00Z-<seq>.jsonl` = canonical manifest line + event lines, written to
  the compliance then the governance bucket (`app/audit_export/archive.py`, SHA-256 checksum, bucket-default Object
  Lock and SSE-KMS), then the manifest row (trigger-enforced chain: seq + 1, previous hash, contiguous period) and the
  registry move in the same transaction. A retry rebuilds the same bytes (same rows, same previous hash); empty hours
  are exported too, so every hour is covered by exactly one manifest.
- **Verify**: `python -m app.audit_export.verify [--tenant <uuid>] [--since <iso>] [--no-objects]` inside the API
  container: one JSON line per agency (codes only), exit 1 on any problem (`content_mismatch` = a row deleted, added
  or changed since the export; `object_missing:<tier>`, `object_mismatch:<tier>`, chain codes, `unexported_rows`,
  `registry_mismatch`). The daily retention runs it incrementally before any drop.
- **Offboarding path**: no DELETE on the audit at all; the agency is locked / trashed, the export keeps running for it,
  its rows leave with the partition drops, then nothing references the tenant row and CRM-35 may delete it (the
  registry cascades, the manifests stay with the seven-year archive).
- **Access log** (`app/audit_log.py`, `app/routers/audit.py`, web `/settings/access-log`): owner only (others: 403
  `role_forbidden` from the matrix, action `access_log_view`, + a committed `denied` event; the web shows why without calling the API); filters = actor group
  (`user`, `ai`, `aa_ops`, `aa_support`, `aa_emergency`, `system`), action, result, member, object, dates inside the
  last 365 days; newest first with a keyset cursor; download as CSV (formula-guarded) or JSON lines, at most 50,000
  rows (422 `export_too_large`). Lines show the member's role and a short id, never a traveller's name; a client or
  trip id links to its page.
- **Metrics** (`AA/Jobs/Crm`, the job sampler): `AuditExportLagMinutes` (Infra alarm at 180),
  `AuditPartitionMonthsAhead` (alarm below 1) and `AuditVerifyLagHours` (largest exported-minus-verified gap over
  agencies; alarm at 50: one agency failing verification stops drops for all).

## F4 roles and team (AA-939, CRM-11)

- **Permission matrix** (`app/permissions.py`, design v3 §4.6): every agency action x owner / agent / assistant with
  rules ALLOW, DENY, OWN, ASSIGNED, THRESHOLD (refund threshold from `tenant_settings`); one table, pinned cell by cell
  in `tests/unit/test_permissions.py`. Doors: `tenant_tx(action=)` (role level) and `permissions.require()` (object
  facts). Membership writes are also refused by the database for anyone but an owner (crm-008).
- **Margin serializer** (`app/margin.py`): `class_router(AGENCY)` builds every agency router with
  `MarginFilteredRoute`; JSON responses lose every key named like net / b2b / cost / margin / commission unless the
  role holds `view_margin` (owner, agent). Unknown role = filtered. Contract test over every `/crm/v1` route:
  `tests/unit/test_margin_filter.py`.
- **Partner pool** (root ADR 0006b, AA-CIS-Infra `identity_partner.tf`): admin-create only, MFA required (password +
  authenticator; passkeys later), the `partner-web` client. The web's pool is chosen by Infra
  (`crm_partner_signin_enabled`); the API accepts both pairs on Dev meanwhile.
- **Invitations** (`app/members/invitations.py`, `app/members/team.py`): the owner invites an address with a role ->
  AA system mail (`app/members/mail.py`, SES) with `/join?t=<token>` (only the SHA-256 is stored, expires after
  `INVITATION_TTL_HOURS`) -> claim (public; `AdminCreateUser` SUPPRESS with a temporary password shown once by the web,
  or the existing account) -> managed login (own password, authenticator) -> accept (the caller's token must carry the
  claimed `(iss, sub)`). The API's partner-pool calls (`app/members/directory.py`) are AdminCreateUser, ListUsers and
  AdminUserGlobalSignOut (IAM allows AdminDisableUser too; unused).
- **Team** (`/crm/v1/team*`, owner): members (invited address or short id), pending invitations (masked), role change
  (409 `last_owner`), removal (membership disabled + global sign-out), the **reassign queue** (open trips / clients of
  members who are no longer active) and reassign, the plan (`/crm/v1/plan`; no `tenant_entitlement` row = free, 10
  seats).
- **Owner recovery** (Q1 6.3, `app/members/recovery.py`): opened by AA (`/crm/ops/v1/.../owner-recovery`,
  `crm-platform-admin`) for an existing member, completes only after 14 days; every member sees the banner and may
  cancel (`/crm/v1/recovery`). AA's member deactivation can leave an agency without an owner: then only a recovery
  makes one.
- **Web**: `web/app/(crm)/settings/team` (owner page, `components/team/TeamAdmin.tsx`), the recovery banner in the
  `(crm)` layout, the join pages `web/app/join/*` (`lib/invite.ts`: token and claim answer in sealed cookies on
  `/join`, never in a URL). Runbooks (lost second factor, rollout order): `docs/implementation-notes/AA-939.md`.
## L1 console (AA-941, CRM-13a)

Design v3 S-01 (CIS admin "Agencies (CRM)": usage meters only, never trips or revenue, no PII), Q1 6.3, Q2 3.12, §4.9
"AA ops reads". The CIS page and its relay are AA-CIS-App's; this repo serves `/crm/ops/v1` (`app/routers/ops.py`,
operations in `app/ops/console.py`):

- **Reads** (`cis-admin` or `crm-platform-admin`): `GET /console` (`{can_write}`), `GET /tenants` (keyset page of
  TenantSummary: id, agency name, state, synthetic, has owner, holds travellers, created; the ids come from the crm-007
  registry function, then one `platform_tx` per agency), `GET /tenants/{id}`, `/usage` (seats, active members 30 d,
  storage, AI spend of the month vs budget as strings, failed jobs 24 h, last activity; `inbound_mail_30d` is 0 until
  CRM-17), `/entitlements`, `/data-requests`, `/jobs` (failed per-agency jobs of 7 days, no payload), `/members`.
  Each agency read writes one `ops_read` event (reason list / summary / usage / ...) in that agency's audit.
- **Writes** (`crm-platform-admin`, ticket reference `^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,10}$` as the audit object id
  `ticket:<REF>`): create agency (`app.agencies.provision_agency` create-only, no member; 201, an existing id 409 `tenant_exists` with nothing written), lock / unlock
  (active <-> locked; a no-op writes nothing), first-owner invite (409 `owner_exists` / `owner_invite_pending` /
  `has_travellers` (any traveller row incl. trashed, or any stored inquiry) / `member_limit_reached`; the shared
  `team.create_invitation`; the address never stored in clear, logged or returned), allowed features (the six crm-008
  codes; never `tenant_settings`: the owner switches on), insights opt-out (`tenant_entitlement.insights_opt_out`,
  crm-011), AA data requests (`agency_export` / `agency_deletion` -> a `crm.erasure_request` the owner must confirm in
  Settings > Data and privacy; no job until then; crm-011 trigger CRM18), job retry (failed `crm_intake_process` /
  `crm_dispatch` / `crm_offboard` of this agency only, 404 `job_not_found` otherwise; locked agency 409), owner
  recovery open (ticket required) / complete, member deactivation.
- **Response allow-list** (`app/ops/serializer.py`, `OpsAllowListRoute` set by `class_router()` for OPS): every key at
  any depth must be listed and match no forbidden pattern (trip, pipeline, revenue, margin, commission, GMV, booking,
  invoice, payment, price, amount, net, proposal, personal-data names); 500 on test / local, dropped + logged
  elsewhere; validation errors answer `{detail: invalid_request, message, fields}` (names only, never the input).
  `tests/unit/test_ops_contract.py` calls every ops route (success and 4xx) and walks the OpenAPI response schemas.
- **Agency side**: an AA-raised request shows on Settings > Data and privacy with Confirm
  (`POST /crm/v1/lifecycle/offboarding/{id}/accept`, owner) / Decline (cancel); a locked agency shows "This agency is
  locked by AA" on the chooser. The opt-out is stored, not yet honoured: the insights card (AA-966) must exclude it.

## Data lifecycle (AA-942, CRM-14)

Design v3 §3.5 "Trash, delete block, archive" (R21, R23, Fix B7), §3.6 retention, §4.10 erasure (Fix B16), V1.

- **Rules** (`app/lifecycle/rules.py`): one row per data class (window, mechanism, enforcing job or card), returned by
  `GET /crm/v1/lifecycle` and worded on Settings > Data and privacy (`web/app/(crm)/settings/data`, EN default, VI).
  Codes only from the API; a unit test checks the web has words for every rule and mechanism.
- **One deletion path**: `crm.erase_subject(scope, subject, profile_token, dry_run)` (crm-010) is the only hard delete
  of agency data (scopes trip, traveler, task, note, document, relation, tenant), reached as `crm_eraser` through
  `app/lifecycle/erase.py`. It refuses under a legal hold (CRM10 -> 409 `legal_hold`), a blocked trip (CRM11 ->
  `delete_blocked`), a client on a live trip (CRM12), a tenant not `offboarding` (CRM14), a profile token that is not
  the traveller's (CRM15), and after a tenant erase any row left in a tenant table outside the keep list (CRM16): a new
  tenant table must be named in the function in the migration that creates it (integration test). The append-only
  consent, stage history and LLM-call tables open to DELETE only inside it (crm_owner + `app.erasing`); the audit never.
  Document objects are deleted from the bucket after the rows, before the commit.
- **Trash** (`app/lifecycle/trash.py`, `DELETE /crm/v1/{trips,travelers,tasks,notes,documents}/{id}`, `GET /trash`,
  `POST /trash/{kind}/{id}/restore`): window = `tenant_settings.trash_days` (default 7); restore inside the window by
  the owner or whoever deleted it. Who deletes: trip owner / agent (own or unassigned trip); client owner only (R8);
  task, document owner / agent; note owner or author. Nothing whose erase the hold rule refuses is moved to the trash
  (409 `legal_hold`, `crm.legal_hold_blocks`, every kind): its purge could never run.
- **Delete block**: `crm.trip_commitment` is the ledger the money, signature, commission and AA-line cards write
  (CRM-16/19/23/25/27): `has_money` (paid / claimed invoice, succeeded or refunded payment), `has_signature`,
  `has_commission` (claimed / received), `has_aa_line` (held / confirmed) -> 409 with the first code, every blocker and
  `alternative = cancel_trip`. Released, expired, cancelled and declined AA lines do not block.
- **Restore re-checks Booking**: an AA line the ledger still shows open goes through `app.state.booking_checker`
  (`app/lifecycle/booking.py`, the seam CRM-19 fills); none configured = 503 `booking_not_configured`, the trip stays in
  the trash.
- **Archive** (R23, `POST /travelers/{id}/archive|unarchive`): hidden from lists and search; queueing a message to an
  archived client is refused; a message queued before is **dropped at the dispatcher** (`crm.dispatch_queue`,
  `crm_dispatch`, `app/dispatch/`). A repeat inquiry unarchives the client. Auto-archive after `auto_archive_months`
  without trip activity; the owner then decides on the Settings page.
- **Legal holds** (`crm.legal_hold`, `POST /lifecycle/holds`, `.../release`): agency, trip or client, under ONE hold
  rule (crm-010 header item 4) that every deletion path uses: a trip is held by a hold on it or on a traveller on its
  party (`crm.hold_trip`); a traveller by a hold on them or on a trip whose party they are on (`crm.hold_traveler`,
  one level); the agency hold holds everything; a row is held when its trip or traveller is (`crm.hold_row`; a
  relation: either end; a referrer link: the referred client). An erase that would delete or update any held row
  refuses with CRM10, dry run too, nothing in part (`crm.erase_held`); `crm.legal_hold_blocks(scope, id)` is the same
  test ("the erase would refuse"), so the trash pre-check, the restore, the daily redaction / purge and offboarding
  (any active hold) agree. `crm.erase_subject`, `crm.lifecycle_purge_expired` and the daily run take
  `LOCK TABLE crm.legal_hold IN SHARE MODE` first, so a hold placed meanwhile waits and a hold being placed is seen;
  `erase_subject` then locks its trip / traveller row FOR UPDATE. The database also refuses changing a held row on
  every other path (rule 4h, `crm.guard_held_change` BEFORE DELETE OR UPDATE triggers on trip, traveler,
  trip_traveler, trip_party, note, document, traveler_relation, form_submission, task, dispatch_queue,
  traveler_health): delete, trash or unlink / content change -> CRM10, which `tenant_tx` maps to 409 `legal_hold`
  for any route (remove from a party, delete a document, remove a relation).
  A hold placed by `platform:*` is released only by `platform:*`; a hold is never deleted nor trashed (its trash
  columns stay NULL), it only moves to released.
- **Erasure** (`POST /travelers/{id}/erasure`, owner): dry run by default; a real run bypasses the trash and leaves an
  `erasure_request` receipt (backup_purge_by = +30 days). CRM-35 (AA-967) adds partial erasure with the signed-agreement
  and accounting exceptions and the data-request log.
- **Offboarding** (`/lifecycle/offboarding`, owner; `app/lifecycle/offboarding.py`): request (dry-run counts) -> export
  job (JSON lines, personal fields decrypted, indexes / F2 pointer / secrets left out, sealed with the agency `export`
  DEK, documents bucket `exports/tenants/<tenant>/<request>.jsonl.enc`, 7-day bucket rule) -> download (audited) ->
  typed confirmation (the agency id) -> agency `offboarding` -> erase job: `erase_subject('tenant')`, objects deleted,
  `KeyProvider.destroy` (crypto-shred; other tasks' DEK caches expire within `DEK_CACHE_TTL_SECONDS`, stated on the
  receipt), agency `offboarded`. The audit and its export registry / chain stay (AA-940 offboarding path). An erase
  the database refuses (a hold placed after the confirmation, CRM16) deleted nothing: the request is `failed`, the
  agency is `active` again and the owner cancels the request. An erase job that meets DRY RUN (switched on after the
  confirmation) is handled the same way (`failed` / `deletion_dry_run`).
- **DRY RUN**: `LIFECYCLE_DRY_RUN` (default true) makes the daily run count only and every owner erasure / offboarding
  erase answer 409 `deletion_dry_run` with the counts.
- **Backups (V1)**: AA-CIS-Infra's snapshot expiry function publishes the oldest backup copy per instance; an alarm
  fires above 30 days (`rds_snapshot_expiry.tf`, AA-942).

## F3 PII scrubber (AA-938, CRM-10)

- **Three passes** (`app/privacy/scrubber.py`, design §4.5), every LLM prompt through `LlmGateway.call(...,
  known=...)`: (1) **known values** of the trip (`app/privacy/known.py`: travellers, companions, the travellers they
  are related to (crm-006 relations, one hop, not on the trip), emergency contacts, opened through the vault, one
  `scrub_known_values` audit event with the traveller ids) -> numbered placeholders
  `<PERSON_1>`, `<EMAIL_1>`, ...; names are matched folded (no diacritics, any case: "Nguyen Van An" = "Nguyễn Văn
  An"), a full name and any run of 2+ of its words always, a given name alone with a capital or after an honorific
  ("chị hoa"), a family name alone only after an honorific (anh, chị, ông, bà, Mr, Mrs, ...), never one word inside a
  known place name ("Bình" in
  "Ninh Bình", `app/privacy/places.py`); a place-like person ("Florence Lyon", "Hà Giang") is still caught because the
  pass matches what the agency stored; (2) **regex** (`app/privacy/patterns.py`): signed links, JWTs, e-mail, dates of
  birth (cue or 2+ years back), Luhn cards, CCCD, passports, phones incl. Vietnamese formats, OTPs; UUIDs never
  touched; digit runs are tried in windows of at most 19 digits, so the cost is linear (passes 1-2 also run in a
  worker thread under the scrub deadline, and the log path uses the same detectors); (3) **Bedrock Guardrails**
  `ApplyGuardrail` in detect mode (`app/privacy/guardrails.py`, acc2 us-west-1, AA-930 §2), `match` never logged.
  Every finding of a mapped kind is replaced: located across whitespace, case, word boundaries and diacritics, the
  placeholders inside a finding kept and every other fragment replaced, overlapping findings merged; a finding that
  cannot be located (or has no value) refuses the scrub. **Typed places** (option C, S249, scrubber `f3-2`): an
  `ADDRESS` finding that is exactly one known destination (`app.privacy.places.is_place`, folded: "Hà Giang", "Hoi
  An", "Huế") is still replaced, as `<PLACE_n>`, so the model knows a place stood there; it is sealed in the same map
  and restored with the rest. A place plus anything else stays `<ADDRESS_n>`; a `NAME` finding stays `<PERSON_n>`
  even when it equals a place; a person named like a destination whom the guardrail labels `ADDRESS` is typed
  `<PLACE_n>` (still masked; the place hint is the accepted cost of option C). **Trip destinations in clear**
  (option B, S250, scrubber `f3-3`): a `NAME` or `ADDRESS` finding that is exactly one of the trip's own destinations
  stays in clear. Scope: the trip's countries (`crm.trip.destinations`, read by `known_values_for_trip` into
  `KnownValues.destinations`) and within them only the explicit list `app.privacy.places.KEEPABLE_PLACES`, matched
  with diacritics ("Hạ Long" yes, "Hà Long" / "Ha Long" no). Never kept: a place that reads as a person's name
  (`app.privacy.names.name_like`: first word a family name or an honorific, or every word a common given name, so
  "Hà Giang", "Mai Châu", "Ninh Bình", "Long An", "Thanh Hóa", "Huế" stay masked; a test holds the list to the rule and
  `is_keepable_place` checks it again), a place right after an honorific or a person word, with any punctuation or
  none between ("chị Sa Pa", "guest Hội An", "Mr.Hội An", "(Mrs) Hội An", "My wife Hội An", "Name: Nha Trang"), a
  place right next to a family name, a middle name, a placeholder or a masked span ("Lê Hội An", "Nguyễn Thị Nha
  Trang", "<PERSON_1> Hội An"), a place plus anything else, and any merged span with a piece that is not keepable. No destinations = `f3-2`
  output byte for byte. The receipt counts kept findings as `PLACE_KEPT` (not in `entities`). Accepted residual risk:
  a person nobody stored whose whole name is exactly a listed non-name-like destination of the trip's countries is
  left visible when the guardrail tags them and no person cue or name part stands next to them. The scrubber `VERSION` is bumped whenever the same input can scrub
  differently.
- **Fail closed**: any error -> `scrub_failed`, a scrub over its deadline (`SCRUB_DETECTOR_TIMEOUT_MS`, default 3 s,
  passes 1-3 together) -> `scrub_timeout`; no call, `manual_reply_needed`. No guardrail configured = every scrub refused. Text that already
  holds a placeholder-shaped token is neutralised to `[KIND_n]` first.
- **Receipt** (`crm.llm_call.scrub_receipt`): scrubber version, detector, counts per kind and per pass, nothing else
  (crm-009 CHECK closed at every level: keys, version codes, numbers, the keys of `counts` and `passes`).
- **Placeholder map** (`app/privacy/placeholder_store.py`, `crm.placeholder_map`): sealed under the tenant `scrub` DEK
  before the call and checked against the crm-009 size / version bounds, stored against the call after the log in a
  savepoint (a failed store keeps the paid call logged and returns an `error`, `manual_reply_needed`), expires after
  24 h (reads refuse it at once, the hourly `crm_placeholder_purge` deletes it). `restore_placeholders` only in a member's `TenantContext` (a job context is
  403), 410 `placeholder_map_expired` after 24 h, one `placeholder_restore` audit event per restore. Model output is
  kept with placeholders.
- **Logs**: the JSON log renderer and Sentry scrubber (`app/observability/scrub.py`) also run the pass-2 detectors
  (cards, CCCD, phones without `+`, passports, OTPs, signed links; not dates).
- **Corpus gate** (`api/tests/scrub_corpus/`): 254 synthetic EN/VI samples (generator + `--check`; each carries the
  trip's `destinations`), 0 leaks, over-scrub of `keep` strings <= 5 %; a `keep` string back once only the
  `<PLACE_n>` placeholders are restored is a **typed place**, reported apart (`typed_place`, `typed_place_rate`), not an
  over-scrub; findings left in clear as trip destinations are counted as `kept_place`. Offline half (passes 1-2; a
  detector that tags every destination `ADDRESS`, one that tags every destination `NAME`, and a replay of the live
  run's labels) in every PR's Unit Tests; all three passes in the workflow **Scrub corpus** (nightly 20:30 UTC and
  on every push to main that changes `api/app/privacy/**` or the corpus; GitHub environment `scrub-corpus`, OIDC role
  with `bedrock:ApplyGuardrail` only; about $0.72 a month). A red run opens or comments on the issue "Scrub corpus is
  red on main".

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
`RATE_LIMIT_INTAKE_FORM_PER_HOUR` (default 200 per form), and the documents bucket (AA-937) `DOCUMENTS_BUCKET`, the
audit export (AA-940) `AUDIT_BUCKET_COMPLIANCE` / `AUDIT_BUCKET_GOVERNANCE` (both or neither) and
`AUDIT_RETENTION_DRY_RUN` (default true), and (AA-939) `COGNITO_PARTNER_POOL_ID`, `PARTNER_WEB_ORIGIN`,
`SES_FROM_ADDRESS`, `SES_CONFIGURATION_SET`, `INVITATION_TTL_HOURS` (default 168), `RATE_LIMIT_CLAIM_PER_MINUTE`
(default 10), and the F3 guardrail (AA-938) `SCRUB_GUARDRAIL_ID`, `SCRUB_GUARDRAIL_VERSION`,
`SCRUB_GUARDRAIL_REGION`, `SCRUB_DETECTOR_TIMEOUT_MS`, and the data lifecycle (AA-942) `LIFECYCLE_DRY_RUN` (default
true). Meanings and defaults: `api/app/settings.py`. Live check of the key provider inside the
container: `python -m app.vault.selftest` (KMS round trip under the nil tenant context, nothing stored).

## Domain terms

- **Permission matrix**: the table of what each CRM role may do (`app/permissions.py`); **margin keys**: response keys
  only owner and agent see. **Invitation**: a single-use, expiring token mailed by AA; claimed (account created or
  found) then accepted by that account. **Owner recovery**: AA promoting an existing member to owner after 14 days.
  **Reassign queue**: open trips and clients whose owner left.
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
- **Access log**: the owner's view of the agency audit (`crm.audit_event`), 12 months in the app plus download.
  **Export manifest**: one row of `crm.audit_export_manifest`, the hash-chained record of one exported hour.
- **Trash**: a deleted object, restorable for the agency's `trash_days`, then purged. **Delete block**: a trip with
  money, a signature, a commission or a held / confirmed AA line cannot be deleted, only cancelled. **Archive**: a
  client hidden from lists and never messaged, history kept. **Legal hold**: a hold that suspends every deletion of
  its subject. **Erasure**: an immediate hard delete of one client (bypasses the trash). **Offboarding**: an agency
  leaves: export, then erase and crypto-shred.
- **Partner host / client host**: the agency app's host (Dev `partner-dev.`) and the travellers' host (later
  `go-dev.`); never one session across both.
