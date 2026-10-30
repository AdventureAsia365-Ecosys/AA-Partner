---
id: ARCH-06-partner
title: "AA-Partner — Repo Context"
description: Stack, boundaries, ownership, route classes, tenancy rules and working rules of AA-Partner (partner CRM API, crm.job worker, CRM web, and the tenant Content studio moved from AA-CIS-App); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-crm-api, aa-crm-web, aa-crm-portal]
last_reviewed: 2026-10-28
review_every_days: 90
related: [ARCH-01, ARCH-06-booking, docs/adr/README.md]
---

# AA-Partner — Repo Context

AA-Partner is the fourth app of the Adventure Asia ecosystem: the CRM that AA gives to partner travel agencies to run
their own B2C clients, with live AA supply inside it and privacy by architecture, plus the tenant Content studio
(`portal/`, moved here from AA-CIS-App by CRM-P2a, AA-979, 25/10/2026), so that after P2b an agency has one host, one
sign-in and one sidebar. Today the repo holds the **walking
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

Architecture diagram (AWS icons): `AA-Ecosys/docs/architecture/diagrams/partner.svg`, drawn from `partner.py` in the
same folder; the catalog page `AA-Ecosys/docs/architecture/diagrams/README.md` holds every view and is shown in the CIS
admin at `/admin/docs?path=docs/architecture/diagrams/README.md`.

- **Owns** schema `crm.*` on the shared acc2 RDS (`aa-cis-dev-db`, PostgreSQL 15), migration key `crm-NNN`, owner role
  `crm_owner`, runtime role `crm_app` (NOINHERIT, connection limit 6, pool 3), and the NOLOGIN readers
  `crm_ai_reader`, `crm_insights`, `crm_support_reader`, `crm_eraser` that `crm_app` reaches only by `SET LOCAL ROLE`
  inside `tenant_tx()` (roles from AA-CIS-Infra `roles/003`, `roles/004`, AA-932).
- **Owns** the agency's data: clients (travellers), trips, proposals, the agency's own suppliers and money records,
  CRM roles (owner / agent / assistant) and the agency audit (ADR 0010 decision 5). The agency is the controller, AA
  the processor (ADR 0008a decision 1).
- **Consumes** only through versioned HTTP contracts (root `docs/contracts/`): `booking.b2b.v1` (AA-Booking),
  `catalog.http.v1`, `tenant.membership.v1` (AA-CIS-App). Service calls use the M2M pool (ADR 0006b decision 7):
  `app/cis/m2m.py` asks the aa-m2m pool for one scope per call (AA-978). `tenant.membership.v1` is pulled by
  `crm_membership_pull` (section "CIS membership feed" below); CIS is a revocation and lifecycle source there, never a
  positive gate and never a source of CRM roles.
  AA-947: `booking.b2b.v1` (quotes, holds, confirms, the `/events` feed) through `app/booking/` and `catalog.http.v1`
  (the AA tour cache) through `app/catalog/`, with the same M2M client (section "CRM-19" below). AA-951: `catalog.http.v1`
  1.2 `/aa-content` (AA prep packs and starter templates, AA-977) into `crm.aa_content` (section "CRM-22" below).
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
| Runtime | ECS Fargate **ARM64** service `aa-crm-dev-api` on `aa-cis-dev-cluster` (acc2, us-west-1); desired 1, running 1 on 25/10/2026 |
| Edge | CloudFront `partner-dev.` -> VPC origin -> internal NLB -> ALB host + `X-AA-Origin` + `/crm/*` rule -> the service (AA-931 spike, ADR 0010 decision 2) |
| Image | ECR `aa-crm-dev-api`, IMMUTABLE, tag = git SHA, built natively on an arm64 runner |
| Web | `web/`: Next.js 16 standalone (React 19, TypeScript, vitest), the AA UI kit core vendored from AA-CIS-App (`web/app/_kit/core/`, root ADR 0016; inline styles from the kit tokens); ECS Fargate **ARM64** service `aa-crm-dev-web` (port 3000, `/healthz`), ECR `aa-crm-dev-web` (AA-CIS-Infra `crm_web.tf`); desired 1, running 1 on 25/10/2026 |
| Portal | `portal/`: the tenant Content studio (Next.js 16, React 19, TypeScript), moved from AA-CIS-App with its history by CRM-P2a (AA-979). Fargate service `aa-crm-dev-portal` on the agency host `portal.lumiguides.it.com` (shared with the CRM web) since the P2b cutover; partner-pool sign-in only since the P2b clean-up; section "Tenant portal" below |
| Edge for the web | the same CloudFront distribution: the ALB rule `crm_api` (`/crm`, `/crm/*`) stays above `crm_partner_default`, which forwards every other path of the partner host to the web target group |

AWS names follow the CRM naming contract of AA-CIS-Infra `cicd_crm.tf`: everything `aa-crm-<env>-*`, tag `App = crm`
(the product is AA-Partner, the resource prefix is `crm`).

## Route classes (ADR 0006b decision 6)

| Prefix | Accepts | Then |
|---|---|---|
| `/crm/health`, `/crm/health/ready` | no token (rate limited per `CloudFront-Viewer-Address`, IPv6 per /64) | liveness / readiness |
| `/crm/public/v1` | no token: the inquiry form only (AA-936), served on the **client host** too (ALB rule `/crm/public/*`) | `GET forms/{key}`, `POST forms/{key}/submissions`; the form key decides the agency inside `intake_tx()`; Origin check, honeypot, consent, per-address limit (before any read) and per-form quota (charged only after every check), body streamed and capped at 32 KiB (413) |
| `/crm/ci/deploy-gate` | the Deploy Dev workflow's GitHub OIDC token only (`app/auth/ci.py`) | the deploy guard: unsafe only while a `DEPLOY_BLOCKING_KINDS` row runs or is queued due within 15 min (AA-993 G5; the set is empty since G4b); job counts as information |
| `/crm/public/v1/invitations/claim` | no token (AA-939): the invitation token in the body only; rate limited per address (`RATE_LIMIT_CLAIM_PER_MINUTE`) | `invitation_tx()`; creates or finds the partner-pool account |
| `/crm/public/v1/s/{token}` | no token (AA-943): the signed client link in the path only (43-character token, SHA-256 stored); served on the client host | `client_link_tx()`; GET the Stage 2 planning form (pages, branches, F2 prefill), POST the answers (Origin check, per-address limit, 32 KiB cap); 404 `link_not_found` for any unusable token |
| `/crm/public/v1/p/{token}`, `/crm/public/v1/portal/{token}` | no token (AA-946): the signed link in the path (purpose `proposal` / `portal`), plus in the portal the session cookie `aa_portal_s` a verified e-mail code set (httpOnly, Secure, SameSite=Strict, Path = that portal's own path, 12 h; SHA-256 stored); served on the client host | `client_link_tx()` of the purpose; the proposal page and accept (409 `price_changed` / `terms_changed`), the portal home, codes, session, Stage 3 (403 `stage3_locked` before the deposit), e-sign v0; writes: Origin check, per-address limit, 32 KiB cap; 404 `link_not_found` for any unusable link |
| `/crm/public/v1/proposal[/pick\|/hold\|/accept\|/change-request]` | no token in the URL (AA-950, Fix B11): the host-only cookie `aa_proposal_link` the web set when the client opened `/c/p/<token>` (httpOnly, Secure, SameSite=Strict, Path `/crm/public/v1/proposal`); served on the client host | `client_link_tx()` of purpose `proposal` (`app/proposals/public.py`); C-03 / C-04 / C-12 of a builder version (Keep / Swap, Choose later, the hold set, accept 409 `hold_expired`, Ask for a change); writes: Origin check, per-address limit, 32 KiB cap; no cookie or an unusable link: 404 `link_not_found` |
| `/crm/v1` | PARTNER x {AA-Partner web} only (the Dev EXTERNAL x {`cis-portal`} bridge was removed at AA-979 P2b: its tokens are 401) | `X-AA-Tenant-Id` + `tenant_tx(action=...)` (membership in `crm.member`, then the permission matrix); `GET /crm/v1/memberships` and `POST /crm/v1/invitations/accept` need no header |
| `/crm/ops/v1` | WORKFORCE x {`cis-admin`} | group `cis-admin` or `crm-platform-admin` reads, `crm-platform-admin` writes (AA-939: member deactivation, owner recovery; AA-941: the L1 console, section "L1 console" below). Every response passes the L1 key allow-list (`app/ops/serializer.py`). AA-979: `POST /tenants/{id}/studio-invitations` (the CIS staff relay of a Content studio invitation) takes any OPS caller, `cis-admin` included (section "Content studio invitations"). The Dev-only E2E CIS admin (root ADR 0006a A10b; a `cis-admin` token whose `email` claim is its address, `Caller.e2e_read_only`, `app/auth/e2e_staff.py`) is read-only: every `crm-platform-admin` write answers it 403 `e2e_read_only` |
| `/crm/staff/v1` | WORKFORCE x {`crm-staff`} | group `crm-ops` or `crm-support` (+ an owner grant for support, CRM-13b) |

- One verifier (`app/auth/identity_verifier.py`, **byte copy** of AA-CIS-App `api/core/identity_verifier.py`, checked
  by CI job "Vendored CIS copies") holds every configured `(issuer, client)` pair; the class guard
  (`app/auth/route_guard.py`) then checks the pair against the class: no token or a bad token = 401, a good token of
  another class (`cis-admin`, `breakglass`, a partner token on ops, ...) = **403**. `breakglass` is accepted by no
  class. The guard sits on the router (`class_router()`), and a unit test calls every served path of each class with
  foreign tokens.
- Issuers and client ids come **only from the environment** (Terraform task definition); there are no code constants
  (public repo, DM-6.10). An unconfigured class accepts nothing. The EXTERNAL pool is configured nowhere since AA-979
  P2b (the Dev `cis-portal` bridge is gone; `COGNITO_EXTERNAL_ISSUER` / `COGNITO_CIS_PORTAL_CLIENT_ID` are no longer
  read), so an EXTERNAL token is unknown to the verifier: 401 on every class.
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
- **`client_link_tx(pool, token_hash_hex)`** (AA-943) is a client page opened from a signed link (the Stage 2
  planning form C-02): it sets `app.client_link_hash`, read by the crm-012 SELECT-only policy
  `client_link_token_lookup` (TO `crm_app`, live links only: not expired, revoked or trashed); the link decides the
  agency (must be active) and `app.actor` = `client_link:<id>` (audit actor kind `traveler`); the row is re-read
  `FOR UPDATE`. Any unusable token is 404 `link_not_found`. Only `app/intake/stage2_public.py` may open it; AA-946
  adds the purposes `proposal` (`app/proposals/public.py`) and `portal` (`app/portal/public.py`), each module opening
  only its own purpose.
- **`ingest_tx(pool, local_part)`** (AA-945) is the forward-in mail's context: the recipient's local part (an
  unauthenticated sender chose it) sets `app.ingest_local_part`, read by the crm-015 SELECT-only policy
  `ingest_address_lookup` (TO `crm_app`, active and live addresses only); the address decides the agency (must be
  active), `app.actor` = `job:crm_mail_ingest`. Unknown, inactive or a locked agency = 404 `address_not_found` (the mail
  is discarded, card R). Only `app/mail/ingest.py` may open it.
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
  and the console (code-rules tests). AA-978: an owner recovery (open / complete) also meets an agency the CIS feed
  locked for want of an owner (`lock_source = cis_membership`), and completing it unlocks the agency; any other lock
  stays 409 `tenant_locked`.
- **`membership_feed_tx(pool, tenant_id)`** (AA-978) is one CIS `tenant.membership.v1` event in one agency: actor
  `platform:feed-tenant-membership` (audit kind `aa_ops`), opened for an agency in any status and also for a CIS
  tenant the CRM has no agency for (`ctx.status` None; the event is still recorded in `crm.membership_inbox`). Only
  `app/cis/membership_feed.py` may open it (code-rules test).
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
  through `Vault.decrypt_secret`, allowed by lint in the dispatcher, webhook and health-check modules and the mail
  sender `app/mail/sender.py` (AA-945; the mailbox health check opens credentials through it); no route
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
| `crm-014` | `api/migrations/014_crm_ledger_suppliers.sql` | CRM-16 agency-line ledger + suppliers (AA-944; needs crm-012 + crm-013): `tenant_settings.base_currency` (default USD, an assumption) / `booking_validation_level` (`off` / `basic` default / `strict`) / `fx_mode` (`automatic` / `manual`); `agency_supplier` (vault-encrypted contact, one built-in Adventure Asia row per agency from `crm.ensure_aa_supplier()`, never changed by crm_app, CRM22; no trash while live lines name it, CRM27); `booking_line` (planning -> confirmed -> cancelled, AA lines only from a `job:` / `platform:` actor, CRM24; an agency line is confirmed only through `crm.booking_line_confirm()`, CRM26; trigger-kept `conf_unique_key` + `booking_line_conf_uq` = the strict duplicate check); `booking_line_money` (append-only chains per line and kind, frozen FX block, RESTRICTIVE `booking_line_money_margin_roles`: owner / agent / job only; writes only through `crm.booking_line_money_put()` / `crm.booking_line_confirm()`; view `v_booking_line_money_current`, security_invoker); `crm.booking_line_confirm_check()` (missing codes only, CRM23 on confirm); `crm.fx_resolve()` (override of the day -> platform of the day -> latest earlier day within 30 days, `stale_days`; CRM25 when none); `fx_rate_platform` (platform reference rates, no tenant, crm_app SELECT + INSERT); `fx_rate_override` (append-only); `crm.erase_subject` = the crm-013 text + the ledger (scopes trip, tenant, and new `booking_line` / `agency_supplier`), `crm.erase_held` + hold guards on the new tables |
| `crm-015` | `api/migrations/015_crm_mail.sql` | CRM-17 Module 5 e-mail (AA-945; needs crm-014): `integration_credential.member_id` + `UNIQUE NULLS NOT DISTINCT (tenant_id, provider, member_id)` (own mailboxes, design §4.6); `channel_account` (one sending mailbox per member or agency-level, gmail / microsoft / smtp, `crm.guard_channel_account` CRM30); `ingest_address` (forward-in local part, one active per agency, Gmail forwarding code; `ingest_address_lookup` on `app.ingest_local_part`); `conversation` (both subjects NULL = Unmatched); `message` (mail-DEK content, `received` or draft_autosaved -> queued -> sent / held_reconnect / failed, sender kinds `agency_mailbox` / `system` only per errata S247, Message-ID hash unique per agency; `crm.guard_message`: crm_app discards drafts only CRM28, a non-draft is sealed CRM29, hold rule through the conversation); `saved_reply`; `mail_import` (counts only); `trip.mail_token` (the `[ref:<token>]` subject tag); `dispatch_queue.message_id` + gate codes `held_reconnect` / `send_failed` / `no_message`; `document.uploaded_by_kind` `mail`; `crm.erase_subject` / `crm.erase_held` = the crm-014 text + the mail tables (scopes trip, traveler, tenant and new `conversation`) |
| `crm-016` | `api/migrations/016_crm_thin_lifecycle.sql` | CRM-18 thin full lifecycle (AA-946; needs crm-015): `agreement_doc` (booking terms / waiver versions, trigger-set `body_sha256`, one published per kind), `proposal` (text under the vault DEK, sell total / deposit / balance date, `price_hash`, accepted with the typed name + pinned hash + terms; one sent and one accepted version per trip), `payment_schedule` (deposit / balance, due -> paid / void; a deposit row is never void), `payment` (append-only manual payments, `crm.ledger_append_only`), `traveler_access` (per-person portal access, Stage 3 time; `crm.guard_traveler_access`: Stage 3 only while `crm.portal_unlocked` holds: a paid deposit or a `manual_booking` override into Booked that was not undone, CRM32), `otp_challenge` (6-digit code hash, 10 min, 5 attempts, the 12 h portal session hash; the code mail is stored masked and the code waits in `code_enc` only until the sender sent it; `delivery` mailbox / dev_echo, where mailbox means sent from the agency's mailbox, whose provider Sent copy its owner can read: e-sign v0 limit; 24 h retention, crm_app deletes), `signature_request` (e-sign v0: typed name, pinned document hash, the signing code, sealed `code_delivery` and `evidence_record` (the canonical evidence JSON, no PII) with `evidence_sha256` tied to it by CHECK, so a signature can be re-verified after the 24 h code purge); `crm.guard_sealed_row` (CRM31: a sent proposal, a paid schedule row, a published agreement, a signed signature never change); client_link purposes `proposal` / `portal`; `crm.enforce_stage_transition` with computable gates (paid deposit -> booked, s3 = 100 -> pre_departure, overrides still logged); `crm.erase_subject` / `crm.erase_held` = the crm-015 text + the new tables (a traveller erase also clears the typed acceptance name, `accepted_name_erased_at`) |
| `crm-017` | `api/migrations/017_crm_aa_lines.sql` | CRM-19 B2B client + AA lines (AA-947; needs crm-016): `booking_line` AA columns (tour and component, quote id / price hash / validity / version, hold expiry, pax counts, "from" price + `below_from_price`, Booking's decline and result codes; NULL on agency lines) and sub-statuses `pending` / `quoted` / `sold_out` / `released` / `not_served` / `failed`; `aa_request` (the outage queue: quote, requote, hold, confirm, release, refresh; pending -> done / failed / cancelled, attempts, backoff, claim lease; one live request per line and kind); `booking_feed_state` (the Booking `/events` cursor per agency); `inbox_events` (append-only, unique per event); `aa_catalog_tour` + `aa_catalog_state` (platform cache of published AA tours); `crm.aa_line_request()` (the only member path: a pending line titled from the cache, CRM33 / CRM34) and `crm.booking_line_aa_money()` (job actor only, frozen at the quote's FX lock); `crm.erase_subject` = the crm-016 text + the new tables; hold guard on `aa_request` |
| `crm-018` | `api/migrations/018_crm_membership_inbox_purge.sql` | AA-978 follow-up (needs crm-013 only; applies after crm-016 / crm-017): `crm.lifecycle_purge_expired()` also deletes the agency's `crm.membership_inbox` rows received more than 90 days ago (DM-7), returned as `membership_inbox`; Guard 0 refuses a purge function a later key extended. No table, grant or policy change |
| `crm-019` | `api/migrations/019_crm_proposal_builder.sql` | CRM-21a proposal model + day-list builder (AA-949; needs crm-017 + crm-018): `proposal.kind` text / builder, `price_visibility` (`total_with_deltas` default / `total_only`, D1), `room_config` (Fix B10), `deposit_pct`, `starts_on`, the version counters `edit_seq` (stale write = 409 `version_conflict`) and `selection_seq` / `based_on_selection_seq` (a client pick made since the working copy was copied is surfaced at publish), status `published` (builder: draft -> published -> sent / superseded / withdrawn; one published per trip), `sent_at` set once (`crm.guard_set_once`, so CRM-21b can send a published version); `proposal_day` (day 0 = Before you go, IANA `tz`, notes under the vault DEK), `option_block` (`client_may_swap` default true, `default_option_id`, `default_pinned`), `option` (agency / AA source, `net_amount` conf:commercial, pay on arrival, choose later agency only, availability, AA quote reference with `aa_nights` (required for a hotel room) and "from"-price warning), `option_rule` (requires / excludes), `selection` (one pick per block of a published / sent version); `crm.guard_proposal_draft` (CRM35: builder rows change only in a draft, an option's availability excepted) and `crm.guard_selection` (CRM36); no ON DELETE actions (a referential action reads every agency's holds), the two circular keys deferred; `crm.erase_subject` = the crm-017 text + the five tables; hold guard on all five |
| `crm-020` | `api/migrations/020_crm_studio_invitation.sql` | CRM-P2b Content studio invitations (AA-979; needs crm-019): `crm.studio_invitation`, a PLATFORM table (no `tenant_id`: the CIS tenant may have no CRM agency; `cis_tenant_id` without foreign key, `cis_invitation_id` UNIQUE = CIS's invitation, re-issued in place), the e-mail sealed under a key derived from the invitation token (the database alone cannot open it), `token_hash`, locale, status pending -> claimed -> accepted -> activated (expired / revoked; expired -> pending on re-issue), the partner-pool identity the claim bound, activation attempts and the last error code; FORCE RLS: a platform actor or the activation job, or the token holder (SELECT + UPDATE on `app.invite_token_hash`); `crm.guard_studio_invitation_write()` CRM37; no reader grant, no DELETE. erase / hold / purge functions unchanged |
| `crm-021` | `api/migrations/021_crm_client_proposal.sql` | CRM-21b client proposal (AA-950; needs crm-019; crm-020 is AA-979's and independent, either order): `proposal` viewed counters (`view_count`, `first_viewed_at`, `last_viewed_at`, `nudged_at`, R4), the version's hold set (`hold_set_ref` = the Booking `proposal_ref` `crm-proposal:<id>`, UNIQUE; `hold_id`, `hold_quote_id`, `hold_expires_at`, `hold_selection_sha256`, `hold_placed_at`, `hold_count`; `recheck_day` / `recheck_count_day` <= 3, Fix A3; the request in flight `hold_attempt`, `hold_pending_key` / `_at` / `_quote_id` / `_price_hash` / `_selection_sha256`; `hold_release_id`, a hold whose release Booking did not answer), the builder acceptance (`accepted_selection_sha256`, `accepted_total_amount`, `accepted_deposit_amount`, `accepted_hold_id`) under the crm-016 acceptance seal; `crm.metric_event` (base template; the bell: `bell_member_id`, `seen_at` set once by `crm.guard_metric_event`, crm_app inserts / reads and has a column grant on `seen_at` only); `crm.erase_subject` = the crm-019 text + `metric_event`. No new SQLSTATE (crm-020 has CRM37; next CRM38) |
| `crm-022` | `api/migrations/022_crm_text_proposal_prep_packs.sql` | CRM-22 text proposal + Before you go (AA-951; needs crm-021): `crm.aa_content` (platform cache of CIS AA-authored content from `catalog.http.v1` 1.2: prep packs and starter templates, one locale per `content_id`, EN / VI paired by `group_key`, `listed`; crm_app SELECT / INSERT / UPDATE, written by `crm_catalog_sync`), `crm.prep_pack_selection` (base template: the packs picked for the Before you go day 0 of one builder version, a copy of the pinned CIS version; draft-only through `crm.guard_proposal_draft` CRM35, trip hold guard), `crm.message_template` (base template: the agency library, body under the mail DEK, `source` agency / aa_starter with one AA copy per group and locale); `crm.erase_subject` = the crm-021 text + the two tenant tables. No new SQLSTATE (next CRM38) |
| `crm-023` | `api/migrations/023_crm_dispatch_attempt.sql` | AA-993 G4b, the `crm_dispatch` send marker (needs crm-022; contract class under ADR 0017: it widens `dispatch_queue_gate_chk` and replaces `erase_subject`): `crm.dispatch_attempt` (base template; one row per dispatcher run that passed the gate, committed before the provider call; `decided_at` + `outcome` sent / held_reconnect / no_message / send_failed / retry / not_sent / send_uncertain; at most one open per dispatch row, `dispatch_attempt_open_uq`; closed once, `crm.guard_dispatch_attempt()` CRM38; `crm_app` SELECT, INSERT, UPDATE (decided_at, outcome)); gate code `send_uncertain`; `crm.erase_subject` = the crm-022 text + `dispatch_attempt` before `dispatch_queue` (a later key starts from the crm-023 text; next CRM39) |
| `crm-024` | `api/migrations/024_crm_invoices_payments.sql` | AA-952 CRM-23 slices 2-3, invoices and payments (needs crm-023; contract class: new constraints on `crm.payment` / `crm.tenant_settings`, `crm.status_transition` rows, replaced `erase_subject`): `tenant_settings` invoice settings (`invoice_prefix` INV, `invoice_tax_label` / `invoice_tax_rate` / `invoice_tax_inclusive`, `invoice_due_days` 7, `service_fee_enabled` false); `crm.invoice_sequence` (gapless +1 under the row lock, CRM40); `crm.invoice` (kind deposit / balance from a schedule row, always tax-inclusive / service_fee / adjustment; draft -> open (number `<prefix>-<6+ digits>`, issue + due date) -> paid, draft / open -> void with a reason; one live invoice per schedule row; sealed once issued CRM31; the issue checks the charge lines against the totals CRM39; the Fix A8 states payment_claimed / paid_unfulfillable / expired exist, unused); `crm.invoice_line` (charge = quantity x unit amount, included = a title snapshot of the trip's agency / AA lines without price; draft only, CRM39); `crm.payment` + `invoice_id`, `provider` manual / stripe / vn_gateway, `provider_ref`, `entry_kind` payment / refund, `refund_of`; `schedule_id` nullable, `payment_schedule_once_uq` partial (payments without an invoice); `crm.refund_request` (proposed -> approved / rejected -> executed, sealed CRM31); `crm.payment_provider_account` (the agency's own methods, one active per provider, manual = pay-by instructions; no secret); `crm.erase_subject` = the crm-023 text + refund_request, invoice_line, invoice (trip and agency scopes), payment_provider_account, invoice_sequence (agency scope) (a later key starts from the crm-024 text; next CRM41) |

Flow (data standards D8 / MIG-4): the file merges here first, then a byte copy goes to AA-CIS-Infra
`db/migrations/crm/` (Migration Copy Guard compares it with this repo's `main`), then DB Migrate `check` -> `apply`
runs it as `crm_owner`, then the `bot/db-schema-reference` PR is merged, then code that reads the new objects deploys.
`/crm/health/ready` stays 503 until every key in `app/db.py` `REQUIRED_SCHEMA_VERSIONS` is registered. Its body lists `registered_keys` (every registered `crm-NNN`, sorted) whenever the schema read succeeds (AA-993 M4a,
root ADR 0017 d7: the coming Deploy Dev `schema-gate` reads it).

## Job runner (`crm.job`, ADR 0003a)

Ported from AA-Booking (AA-751 rules): `FOR UPDATE SKIP LOCKED` claims, guarded state changes, lease + heartbeat,
reaper (a stopped worker's job is re-claimed), release on graceful shutdown, `cancel_requested`, backoff, the backlog
sampler to CloudWatch `AA/Jobs/Crm`. CRM differences: the job row keeps the **error class only** (crm-001 column
comment); recurring kinds are scheduled with an idempotency key per time slot (`<kind>:<slot>`) because crm-001 has no
singleton index. **Only a deploy-gate / DB Migrate exempt kind may be Recurring**: a Recurring kind always holds its
next slot's row queued ahead, and the Infra DB Migrate idle check counts any queued row of a non-exempt kind as busy
(the deploy gate no longer does since AA-993 G5: it blocks only while a `DEPLOY_BLOCKING_KINDS` row runs or is queued
due within 15 min, and lists
queued / running rows as information, exempt kinds left out). Daily or slower kinds are queued **due now** by a worker maintenance hook (Booking's
`DailySchedule` pattern, AA-825), one idempotency key per UTC day.

| Kind | Every | What |
|---|---|---|
| `crm_heartbeat` | 60 s | Liveness. Always queued; exempt from the deploy gate and from the DB Migrate idle check (Infra `owners.yaml`) |
| `crm_intake_process` | per stored inquiry (queued in the submission's transaction) | `app/intake/process.py`: e-mail then phone blind-index dedupe; **attach** to the lead's open trip (`inquiry` / `qualifying`, C47 in R6) or create traveller (F1) + profile (F2) + companions + trip at Inquiry + party twins + "Reply within 2 h" task + consent rows; a payload that does not open marks the submission `failed` (permanent). Not deploy-gate exempt |
| `crm_audit_export` | hourly, queued due now from minute 16 (`HourlyAuditExportSchedule`) | AA-940: keeps the audit partitions three months ahead, then exports every agency's closed hours (15-minute lag, at most 48 per agency per run) to both Object Lock buckets and the manifest chain; `not_configured` without the buckets. Not deploy-gate exempt |
| `crm_audit_retention` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook | AA-940: verifies every agency from `verified_until` (rows, chain, both copies), moves `verified_until`, then drops expired partitions (dry run while `AUDIT_RETENTION_DRY_RUN` is on, the default) |
| `crm_placeholder_purge` | hourly, due-now hook (`app.privacy.purge.HourlyPlaceholderPurge`, key per UTC hour) | AA-938: deletes expired F3 placeholder maps of every agency in one DELETE through the crm-009 purge policy (no read); count only. Not deploy-gate exempt (exists only while due / running) |
| `crm_lifecycle` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook (`DailyLifecycleSchedule`) | AA-942: per agency (active / locked): form-submission payloads redacted after 90 days, LLM calls older than 180 days purged (with their crm-009 placeholder maps), CIS membership inbox rows older than 90 days purged (crm-018), trash past its window purged through `crm.erase_subject`, inactive clients auto-archived; one `lifecycle_run` row + one audit event per agency; skips anything under a legal hold or the delete block; DRY RUN (counts only) while `LIFECYCLE_DRY_RUN` is on (default). Not deploy-gate exempt |
| `crm_offboard` | per owner step (queued in the request's transaction) | AA-942: `mode=export` builds and seals the agency export; `mode=erase` erases the agency, deletes its objects, destroys its keys, writes the receipt. Not deploy-gate exempt |
| `crm_dispatch` | per queued message (after the 10 s undo delay for a user action) | AA-942: the dispatcher gate at send time (trashed, archived, no marketing consent -> dropped; no sender -> held). Not deploy-gate exempt. AA-993 G4b (crm-023): a `crm.dispatch_attempt` row is committed **before** the provider call and closed with the decision; a run that finds an open attempt (stopped between the send and its decision) holds the row `send_uncertain` (message `failed` / `send_uncertain`) instead of sending again, and a run cancelled before the sender was called closes it `not_sent`. So it is **not deploy-blocking** any more (it was from G5 until G4b) |
| `crm_membership_pull` | 20 s, Recurring (next slot queued at each worker maintenance pass, about every 30 s when idle) | AA-978: pulls `<CIS_API_URL>/tenant/membership/v1/events` with an M2M token of scope `cis.tenant/read` (section "CIS membership feed"). Deploy-gate exempt and DB Migrate idle exempt (Infra `owners.yaml`), like the heartbeat; a CIS failure ends the run successfully with the error class in `crm.membership_feed_state` and backs off (20 s doubling to 5 min) |
| `crm_stage_date_rule` | hourly, due-now hook (`app.stages_job.HourlyStageDateRule`, key per UTC hour) | AA-943: per active agency, "today" in `tenant_settings.timezone`: booked -> travelling on the start date (reason `date_rule_gate_unmet` + an urgent task), pre_departure -> travelling, travelling -> completed on the end date, one `return_date_missing` task for a travelling trip without one. AA-946: booked -> travelling says `date_rule` when Stage 3 is complete (s3 = 100, recomputed) and `date_rule_gate_unmet` + the urgent task only when it is not; one `stage3_due_t21` task for a booked trip within 21 days of departure with s3 < 100; deletes e-mail codes older than 24 hours. Counts only. Not deploy-gate exempt |
| `crm_fx_fetch` | daily, due-now hook from 00:00 UTC (`app.fx.fetch.DailyFxSchedule`, key `crm_fx_fetch:<date>`) | AA-944: the platform FX rates in `crm.fx_rate_platform` (`ON CONFLICT DO NOTHING`, the only writer): `FX_PROVIDER=synthetic` a fixed USD table incl. VND (Dev), `live` ExchangeRate-API open access (USD base, VND crosses) + ECB (EUR base), fawazahmed0 stored as a cross-check only (a gap > 1 % is logged and counted). Counts only. Not deploy-gate exempt |
| `crm_mail_ingest` | per UTC minute, only while `raw/` of the ingest bucket holds objects (`app.mail.ingest.MailIngestScan` hook lists one key; payload `{"purpose": "mail_ingest"}`, never an object key) | AA-945: up to 50 forwarded mails per run: parse, `ingest_tx`, thread, store under the mail DEK, keep allowed attachments as documents, delete the raw object; an unknown address is deleted and counted (`MailIngestDropped`, card R). Not deploy-gate exempt |
| `crm_mail_health` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook (`app.mail.health.DailyMailHealthSchedule`) | AA-945: every agency's mailboxes: token refresh / SMTP login + NOOP; passing -> connected (held messages re-queued), revoked -> disconnected + one owner alert, unreachable -> needs attention. Counts only. Not deploy-gate exempt |
| `crm_mail_history_import` | per mbox / CSV upload (queued in the upload's transaction) | AA-945: parses the upload from `imports/<tenant>/<id>`, stores each mail through the ingest path (`imported`), counts on `crm.mail_import`, deletes the object. Not deploy-gate exempt |
| `crm_booking_sync` | 60 s, Recurring, plus a one-agency nudge per AA request (payload `tenant_id`, `aa_request_id`) | AA-947: sends the due `crm.aa_request` rows of every active agency to Booking (`<BOOKING_API_URL>/booking/b2b/v1`, one M2M scope per call; holds / confirms with a stable `Idempotency-Key`), applies the answers (line, money at the quote lock, `trip_commitment`, `waiting_on`), keeps a request pending with a backoff while Booking is down, then pulls the agency's `/events` into `crm.inbox_events` (section "CRM-19"). Deploy-gate exempt and DB Migrate idle exempt (Infra `owners.yaml`), like the membership pull; `not_configured` without `BOOKING_API_URL` / `M2M_CLIENT` |
| `crm_catalog_sync` | hourly, due-now hook (`app.catalog.sync.HourlyCatalogSync`, key per UTC hour) | AA-947: refreshes `crm.aa_catalog_tour` from `<CIS_API_URL>/catalog/v1` (scope `cis.catalog/read`): full passes comparing `content_hash`, a changed tour fetched with `If-None-Match`, tours no longer listed re-checked (410 retired, 404 paused); bounded per run, the pass continues next run; a CIS failure backs off. AA-951: after the tour pass, `crm.aa_content` from `/aa-content` (prep packs, starter templates; a body fetched only when version or hash changed, at most 60 a run; items CIS no longer lists unlisted after a complete list); a content failure is counted (`content_error_count`), never fatal. Not deploy-gate exempt |
| `crm_studio_activation` | per accepted Content studio invitation (queued in the accept transaction, idempotency key per invitation, 12 attempts) | AA-979: CIS `tenant.membership.v1` 1.1 `activateInvitation` with an M2M token of scope `cis.tenant/activate` (`app/members/studio.py`, `app/cis/activation.py`); a CIS outage / 5xx / 401 / 403 retries with backoff, a CIS 404 / 409 fails it permanently with the code on the row. Not deploy-gate exempt |
| `crm_job_retention` | daily, from `JOB_RETENTION_UTC_HOUR` (default 19 UTC), due-now hook | Data standards §7 row `crm.job`: succeeded 30 d, cancelled 90 d; **failed rows kept** until an archive bucket exists; DRY RUN (counts only) while `JOB_RETENTION_DRY_RUN` is on (default) |

## CI and release

- Web checks (AA-935): **Web Build** (kit core integrity + provenance, vitest, `next build` with the type check,
  standalone smoke, kit lint) and **Web Docker Build Check** (native arm64 image, container smoke, Trivy); Security Audit also runs `npm audit` for `web/` and `portal/`.
- Portal checks (AA-979): **Portal Build** (kit core integrity + provenance, `npm ci`, `tsc --noEmit`, `next build`, kit
  lint). The portal deploys from `deploy-portal-dev.yml` (Fargate) only, never from `ci.yml` (its Vercel project and
  workflow went at the P2b clean-up, AA-979 §4.5).
- **Kit lint** (AA-994 S2b, root ADR 0016 decision 7): `portal/` and `web/` each run ESLint 9 with the Next rules plus
  the kit preset `inline` from the vendored `app/_kit/core/eslint-kit.mjs` at `error` (`eslint.config.mjs`; portal:
  `app/**`, web: `app/**` + `components/**`, the vendored core exempt). Violations that existed before S2b are frozen
  in each app's `eslint-suppressions.json` (ESLint bulk suppressions), which only shrinks: the job runs
  `eslint --prune-suppressions`, fails when the committed file differs from the pruned one, fails when an entry grows
  against the base branch (`check-kit-copy.mjs --suppressions … --base-files …`) and fails on an inline disable of a
  kit rule or a bare `eslint-disable` in page code (`--no-kit-disable`). After fixing violations run
  `npx eslint --prune-suppressions` in the app folder and commit the file; never `--suppress-all` to hide a new one.
  The steps are inside **Portal Build** / **Web Build** (required checks), so they gate the merge; the pre-push hook
  runs the dependency-free kit checks and ESLint on the changed files (`scripts/pre-push.sh`).
- Required checks: **Lint**, **Security Audit** (DM-6.10 resource-ID scan, pip-audit), **Unit Tests** (coverage >= 90 %),
  **Integration Tests** (PostgreSQL 15, no skips), **Docker Build Check** (native arm64 build, container smoke, Trivy,
  SBOM); plus **Docs lint**, **PR title**, **gitleaks**, and **Portal Build** + **Web Build** (AA-994 hand step H1, on
  branch protection by 28/10/2026). Non-required: **Vendored CIS copies**.
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
  only the image swapped, then the post-deploy verify.
- Post-deploy verify (AA-993 V2, ADR 0016 D3): `deploy-dev.yml` calls `post-deploy-verify.yml` after `deploy` (also
  dispatchable with `expected_sha`; its own concurrency group, never `deploy-dev`). Script
  `.github/scripts/post_deploy_verify.sh` (byte-identical to AA-Booking's), HTTP rows `.github/verify/crm.rows` (a PR
  that adds an endpoint worth guarding adds a row). Red, never a warning: ECS rollout not COMPLETED, desired count 0
  (no automatic scale-up), a running image other than `<registry>/<repo>:<sha>`, `/crm/health` `git_sha`/`version` off,
  `/crm/health/ready` not 200 or below the newest `REQUIRED_SCHEMA_VERSIONS` key of the deployed commit, or
  `AWS_ROLE_ARN` unset (the `config` job fails). The **Deploy Dev result** job is red when any job did not succeed.
- Post-deploy verify of the CRM web and the Content studio (AA-993 V3): the same workflow with input `app`
  (`crm` default, `crm-web`, `crm-portal`; optional `resolve_via` pins the host to a CloudFront name), called by
  `deploy-web-dev.yml` and `deploy-portal-dev.yml` after `deploy`. Rows `.github/verify/crm-web.rows` (`/healthz`
  `git_sha`/`version`/`auth_configured`; `/crm/health` + `/crm/health/ready` reaching a ready API through the host's
  API rule, without comparing the API's commit: a web deploy does not deploy the API) and `crm-portal.rows` (the
  studio's `/api/healthz` `sha`/`version`: on the agency host `/healthz` is the CRM web's; the API rule), plus
  `.github/scripts/edge_verify.sh` (signed-out redirects to the web's `/auth/refresh`; for the studio a public page
  (`/terms`) and its `/portal-assets`, and `/tenant-login` 308 to the partner sign-in). Same red rules (desired 0, rollout, image). `CRM_WEB_ENABLED` / `CRM_PORTAL_ENABLED` unset is red
  (`config` fails). The portal deploy then runs the Playwright smoke (`ui-smoke-portal.yml`, always the Fargate studio,
  `require_tenant_login: true`: a missing e2e studio sign-in secret `E2EPORTALEMAIL` / `E2EPORTALPASSWORD` /
  `E2EPORTALTOTP` is red). Read **Deploy Web Dev result** / **Deploy
  Portal Dev result**.
- release-please (manifest mode) keeps one release PR; the version is baked into the image and returned by
  `/crm/health`.

## Web (`web/`, AA-935)

- **Hosts** (`web/lib/hosts.ts`, `web/proxy.ts`): the partner host serves the agency app; the client host
  (`WEB_CLIENT_HOSTS`, later `go-dev.`) serves `/c/*` and `/trip/*` only (both 404 on the partner host); any other
  Host is 404; `/healthz` answers any Host (the ALB health check sends the task IP). Cookies are host-only, so the two
  hosts never share a session.
- **Trip page** (AA-979 P2b §4.3, `web/app/trip/[tourId]`, `web/lib/tripPage.ts`): the public AA tour page moved from
  the CIS admin host (which answers 307 to it). Server side it reads CIS's public `GET <CIS_API_URL>/v1/trip/{id}`
  (no token; CIS stamps `url_alive` for its T10 gate): CIS 301 (superseded version) -> 308 to `/trip/<current id>`,
  410 / 404 -> not-found page, a CIS fault or an unset `CIS_API_URL` -> 500. Indexable (page metadata overrides the
  root `noindex`; `public/robots.txt` allows `/trip/` only). Next data cache 1 h.
  **AA-979 P2b window**: the agency host `portal.` is a partner host too (`WEB_PARTNER_HOSTS` lists both
  `partner-dev.` and `portal.`), and each partner host answers with its OWN public origin (`lib/config.ts`
  `forHost` / `requestConfig`): redirects, the OAuth redirect / logout URIs and the same-origin write guard stay on
  the host the person is on, so the DNS cutover of `portal.` needs no web or API change. `WEB_PUBLIC_ORIGIN` is the
  fallback for any other Host. On `portal.` the Content studio link is the same-origin `/portal/dashboard`, a person
  with no CRM agency goes straight to it (a Content-studio-only member), and sign-out also expires the studio's five
  `cis_*` cookies (`lib/studioCookies.ts`): one host, one sign-in, one sign-out.
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
  `data-card`), the Content studio cross-link (`CONTENT_STUDIO_URL`), Settings with language and dates. The portal
  sidebar (`portal/`, AA-979) links back ("Open CRM").
- **UI kit** (AA-994 S2a, root ADR 0016): `web/app/_kit/core/` is a byte copy of AA-CIS-App `frontend/app/_kit/core/`
  (full profile), written only by the root `tools/kit/kit-sync.sh crm-web` and pinned by `web/kit.lock.json`; never
  edit it here. `web/app/_kit/local/` holds the CRM's own parts (the theme binding, key `cis_theme`, and `CrmShell`);
  `web/app/_kit/index.ts` is the CRM's barrel, which pages import. The `--aa-*` palette is `core/tokens.css`, imported
  by `app/layout.tsx` before `globals.css`; no other CSS file may define an `--aa-*` variable. Web Build checks
  integrity (`core/check-kit-copy.mjs`, also `--css`) and provenance (`api/scripts/check_kit_provenance.py`: the copied
  manifest is CIS's at the lock's `ref`, and that `ref` is on CIS `main`). `web/lib/{sameOrigin,crossSiteGuard,bffPath,
  proxyResponse}.ts` stay byte copies of AA-CIS-App `frontend/lib/` (CI "Vendored CIS copies").
- **Configuration** (task environment, AA-CIS-Infra `crm_web.tf`; meanings in `web/lib/config.ts`): `ENVIRONMENT`,
  `WEB_PUBLIC_ORIGIN`, `WEB_PARTNER_HOSTS`, `WEB_CLIENT_HOSTS`, `CRM_API_ORIGIN`, `COGNITO_DOMAIN`, `COGNITO_CLIENT_ID`,
  `CONTENT_STUDIO_URL`, `CIS_API_URL` (AA-979, the trip page; optional), and from Secrets Manager
  `COGNITO_CLIENT_SECRET`, `WEB_SESSION_SECRET`. Missing identity values
  turn sign-in off (the pages say so) while `/healthz` stays up.
- **Deploy**: `.github/workflows/deploy-web-dev.yml` (same model as the API deploy, no job guard: the web runs no
  worker), gated by the `dev` environment variable `CRM_WEB_ENABLED` (unset = red), then the post-deploy verify
  (`app: crm-web`, section "CI and release").

## Tenant portal (`portal/`, AA-979 CRM-P2a)

The Content studio of the tenant agencies, moved here from AA-CIS-App (`portal/`, AA-885) with its history (root ADR
0010 §6, stage P2a: `git log --follow` reaches the pre-split CIS commits; the trailer `Imported-from:` names each
original CIS commit). P2a moves the code only:

- **Host and deploy**: the Fargate service `aa-crm-dev-portal` on the agency host `portal.lumiguides.it.com`, shared
  with the CRM web (P2b, below). The Vercel project of P2a (`aa-cis-portal`) was the rollback target of the cutover and
  was deleted at the P2b clean-up (AA-979 §4.5) with its workflow, `portal/vercel.json`, the root `.vercelignore` and the
  repo values `VERCEL_PORTAL_PROJECT_ID`, `VERCEL_PORTAL_BYPASS_SECRET`, `VERCEL_TOKEN`, `E2E_TENANT_API_KEY`. There are
  no portal PR previews: Portal Build and Portal Docker Build Check are the pre-merge checks, the Fargate smoke the
  post-deploy one.
- **Sign-in**: the partner pool (MFA) through the CRM web on the same host, exchanged at CIS for a short studio token
  (P2b, below). The people-facing API-key sign-in (`/tenant-login`, BFF `portal/app/api/auth/tenant-login`, CIS
  `POST /auth/tenant-login` with `client: "portal"`, unmetered, AA-760) is OFF unless `PORTAL_API_KEY_SIGNIN=true`
  (a local studio only): `/tenant-login` answers 308 to the web's sign-in with `next=/portal/dashboard`, the BFF 404.
  Tenant API keys are for machines. The studio token lives in the host-only cookie `cis_tenant_token`; the gate is
  `portal/proxy.ts` (tenant only).
- **Business logic stays in the CIS API** (ADR 0010 §7): the portal is a client of CIS `/v1/*` and `/auth/*` through
  its BFF (`portal/app/api/tenant/[...path]`, cross-site writes refused first, AA-911). It never reads the `crm`
  schema or calls `/crm/*`; the sidebar only links the CRM home, same origin `/` (`NEXT_PUBLIC_CRM_URL` overrides).
- **UI kit** (AA-994 S2a, root ADR 0016): `portal/app/_kit/core/` is a byte copy of AA-CIS-App
  `frontend/app/_kit/core/` (full profile), written only by the root `tools/kit/kit-sync.sh portal` and pinned by
  `portal/kit.lock.json`; never edit it here (edit the CIS core, merge, then sync). `portal/app/_kit/local/` holds the
  portal's own parts: `api.ts` (the fetch helpers, portal-owned since the CIS file of that name is CIS-local), the theme
  binding (key `cis_theme`) and the portal shell (`PortalShell`, `PortalSidebar`, `PortalTopbar`, `PortalBottomNav`,
  `PortalCommandPalette`, `SessionWatch`, `portalNav`, `useShellData`; moved from `app/portal/_shell`).
  `portal/app/_kit/index.ts` is the portal's barrel. The `--aa-*` palette is `core/tokens.css`, imported by
  `globals.css`; no other CSS file may define an `--aa-*` variable. Portal Build checks integrity and provenance (as
  in the CRM web). `portal/lib/{sameOrigin,crossSiteGuard,bffPath,proxyResponse}.ts` stay byte copies of AA-CIS-App
  `frontend/lib/` (CI "Vendored CIS copies", `PORTAL_COPIES`).
- **Guards**: `api/tests/unit/test_portal_guards.py` (no admin secret or staff session in `portal/`, tenant-only gate,
  security headers, the 308 map of the old slugs, draft legal pages outside the gate, muted-text contrast, the
  unmetered portal session, cross-site write refusal, lockfile tracked, deploy on `portal/**` only).
- **Smoke**: `e2e/smoke-portal/` with `e2e/playwright.portal.config.ts` (host checks always; logged-in pages after
  `auth.setup.ts` signs the e2e studio user in through the partner pool with its authenticator code, secrets
  `E2EPORTALEMAIL` / `E2EPORTALPASSWORD` / `E2EPORTALTOTP`: a synthetic partner identity with a Content studio
  membership on the E2E tenant). `ui-smoke-portal.yml` always checks the Fargate studio and takes `resolve_via` and
  `require_tenant_login` on dispatch and on call; the Fargate deploy calls it with the sign-in required (AA-993 V3).
  A local run against a studio without partner sign-in sets `PORTAL_TARGET` empty and uses `E2E_TENANT_API_KEY`.
- **Account pages (AA-761)**: Billing = plan card, rewrite meter (used / quota and reset date from CIS `GET /v1/quota`,
  overage so far from `GET /v1/billing`) and an invoices empty state (CIS stores no invoices yet); Activity = kit
  DataTable with server paging over CIS `GET /v1/account/activity` (type and UTC-date filters) and a CSV export of the
  whole filtered set (`GET /v1/account/activity/export`); Integrations = each channel's health and test (Publish only
  shows a summary that links here) and the API key (`GET /v1/account/api-key`: masked, issued, last used; a new key
  by email to AA). Pure helpers in `portal/lib/account.ts`, tested by `npm test` (node --test, in Portal Build).
- **Marketplace (AA-671)**: one page at `/portal/tours` for the tours a tenant can pick and the tours it picked (was
  two pages, Browse Tours and Tour overview). View "All tours" (default): CIS `GET /v1/tours/pool` as a card grid with
  server paging and filters (name, country, length; newest first, "New" for the last 14 days), the kit Drawer for one
  tour and the (bulk) rewrite with the Billing quota note (`lib/tours.ts` `rewriteQuotaNote` over `usageMeter`). View
  "In my catalog" (`?view=mine`): CIS `GET /v1/marketplace` (price, status, version); a card opens the tour in My Tours.
  `/portal/tours/marketplace` and `/portal/marketplace` answer 308 to `?view=mine`. Activity / intensity filters wait
  for the CIS pool API to expose them (implementation notes AA-671).
- **The AA-CIS-App copy** of `portal/` was deleted at the P2b clean-up (a CIS guard keeps it gone). Portal changes are
  made here only.
- **P2b (AA-979, prepared S252; `portal.` cut over to Fargate in S253, 27/10/2026)**: the studio runs as the
  Fargate service `aa-crm-dev-portal` (`portal/Dockerfile`: standalone, Next assets under `/portal-assets/`, `/healthz`, the same body
  at `/api/healthz` for the post-deploy verify; deploy
  `.github/workflows/deploy-portal-dev.yml`, CI "Portal Docker Build Check") behind the agency host `portal.` (AA-CIS-Infra
  `crm_agency_edge.tf`: its own CloudFront distribution; the ALB sends `/portal*`, `/api/*`, `/tenant-login*`, `/terms`,
  `/privacy`, `/assets/*` to the studio, `/crm*` to the API, everything else to the CRM web). The cutover switch is the
  `portal.` CNAME only (`crm_agency_live`). Configuration (`portal/lib/config.ts`): `PORTAL_HOSTS` (Host guard),
  `PORTAL_PUBLIC_ORIGIN` (redirects and the write guard `portal/lib/writeGuard.ts`, never the bind address),
  `PORTAL_API_KEY_SIGNIN` (off; on only for a local studio), `WEB_SESSION_SECRET` (the CRM web's session key), `API_URL`,
  `SENTRY_DSN`. **Sign-in** (`portal/proxy.ts`, `portal/lib/tenantSession.ts`): the CRM web's partner session (its sealed cookie, read only: `portal/lib/session.ts` and
  `seal.ts` are byte copies of `web/lib/`, guarded by `test_portal_guards.py`) is exchanged at CIS `POST
  /auth/tenant-session` for a tenant JWT of at most 15 minutes in the same five `cis_*` cookies; several studios ->
  `/portal-access/select`, none -> `/portal-access/none`; no session -> the web's `/auth/refresh`. The BFF renews an
  expiring exchanged token itself and answers 401 + `x-aa-session: expired` when it cannot (the shell reloads once).
  One sign-out: the studio clears its cookies, then posts the web's `/auth/sign-out`. Sentry (`instrumentation*.ts`):
  only with a DSN, no PII. **Clean-up (AA-979 §4.5, S256)**: API-key sign-in off, Vercel deleted, the CIS copy deleted,
  `partner-dev.` answers 301 to `portal.` except `/crm/*` (AA-CIS-Infra CloudFront Function), the CRM API's join-link
  and mail OAuth origins on `portal.`.

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
  API (AA-CIS-App `AppInfo("crm")`). `crm.ops.v1` 1.4 (AA-992): `GET /crm/ops/v1/data-coverage` for the CIS admin page
  `/admin/data-coverage`: agencies per status (+ synthetic), members per status and the AA catalog cache (served /
  paused / retired, last full pass, last ok, failures, last error class). Counts only, no agency id or business
  figure; counted through one platform context per registered agency WITHOUT an `ops_read` event (no agency data is
  read; one event per agency every 10 minutes would bury the owner's access log), logged once per call instead.
- **LLM gateway** (`app/llm/gateway.py`, D12): scrub receipt -> month budget (`tenant_settings.ai_budget_usd`) ->
  `ai_enabled` (default off) -> seal the placeholder map -> call -> one `crm.llm_call` row (no text) + the rollup ->
  store the map; every refusal is logged too and every non-`ok` result carries `fallback = manual_reply_needed`.
  Slice 1 shipped the stub scrubber / provider pair (a stub scrubber is refused with any real provider); AA-938 adds
  the F3 scrubber (below), CRM-33a Bedrock.
- **Not in Slice 1** (later cards): the prospect's confirmation mail and the agent alert (traveller mail leaves the
  agency's own mailbox, CRM-17), system tags from the catalog (R29), conditional forms and the Stage 2 gate (CRM-15),
  F2 views and reader grants (CRM-09), the `form_submission` 90-day redaction job (CRM-14, done in AA-942).
- **E2E** (`e2e/`, workflow `E2E Dev`, dispatch on main): form -> card on the board within about a minute -> trip.
  AA-943 adds `e2e/tests/slice2-m1m2.spec.ts` (call slot, auto-send + Undo, the client planning page, the gates, fill
  in from call, bulk preview, cancel dry run and run).

## CRM-15 Modules 1 + 2 (AA-943)

- **Stage engine** (`app/stages.py`, routes in `app/routers/pipeline.py`): every edge of crm-012 (`ALLOWED_MOVES`;
  `lost` before booked only; `cancelled` only through `POST /trips/{id}/cancel`, the generic move answers 409
  `use_cancel_flow`). Gates: qualifying -> proposal needs Stage 2 >= 80 % or an override reason (else 409
  `stage2_incomplete` `{s2, missing, override_reasons}`); proposal -> booked and booked -> pre_departure need the
  override `manual_booking` / `manual_stage3` until CRM-18 / CRM-24a / CRM-25 (409 `gate_unmet`). The override is
  passed to the DB as `app.gate_override` for the one UPDATE and logged in `trip_stage_history.override_reason_code`.
  The DB refuses on its own too (SQLSTATE CRM19).
- **Undo** (Fix B8): each move returns `undo.history_id` / `undo_until`; `POST /trips/{id}/stage/undo` within 10 s, by
  the member who moved, reverts it (`app.stage_undo`; DB rule CRM20, 15 s) and cancels what the move started in its
  transaction: dispatch rows (`cancelled` / `undone`), signed links (`revoked`), tasks (`skipped`). Later: 409
  `undo_expired`. A cancel has no undo. Bulk: `POST /pipeline/bulk-stage` (dry run -> `eligible`, `blocked`,
  `preview_hash`; the run needs the same hash, else 409 `preview_stale`; at most 200 trips; one `batch_id`) and
  `POST /pipeline/bulk-stage/undo`.
- **Cancel** (C12): `POST /trips/{id}/cancel?dry_run=true|false` `{cancel_reason_code}` (owner / agent), from
  booked / pre_departure / travelling: the dry run lists the effects (dispatch rows cancelled, open tasks skipped,
  future call slots cancelled, links revoked); later cards add theirs.
- **waiting_on** is derived on every move (`derive_waiting_on`); cards register hooks for booked / pre-departure trips
  (`register_waiting_on_hook`, contract in `docs/implementation-notes/AA-943.md`). `last_activity_at` is bumped by
  moves, notes, task changes and submissions; the board flags a trip quiet after 3 days and shows a Closed group.
- **Stage 2** (`app/intake/stage2.py`): the `stage2.v1` pages with R31 branches (adventure-only questions, health after
  the consent); the server drops hidden-branch answers (counted in `hidden_answers_dropped`) and refuses health
  without consent (422 `health_consent_required`). Answers go to the F2 profile, health codes to `traveler_health`,
  free text to an encrypted `stage2_free_text` note, the set to a `form_submission` (stage `s2`, outcome `applied`).
  Sent as a signed `client_link` (14 days, SHA-256 only, a re-send revokes the old one) through the dispatcher
  (held / `no_sender` until CRM-17, so the trip page offers "Copy link" from the send answer), automatically on
  inquiry -> qualifying with `tenant_settings.stage2_auto_send` (never blocking the move: a lead that cannot be
  messaged, or a mover whose `MESSAGE_SEND` rule refuses the trip, gives `stage2_skipped`), or filled in from a call
  (`PUT /trips/{id}/stage2`). A signed link opens only the page of its own `purpose`.
- **Completeness** (`app/intake/completeness.py`): s1 / s2 / s3 percent + missing codes on `trip.intake_completeness`
  and the profile; a Qualifying trip below 80 % has an open `get_planning_info` task, closed at 80 %.
- **Call slots** (R15, `app/intake/slots.py`): weekly windows in the agency timezone (Settings > Forms,
  `GET/PUT /crm/v1/settings/pipeline`); the public form lists free starts (ISO UTC); `crm_intake_process` books one
  (`meeting_slot` requested + a `call_scheduled` task) or, when another inquiry won the race, opens an urgent
  `call_slot_taken` task and keeps the inquiry.
- **Web** (S4): the board has every crm-012 edge between its six open columns (a gated drop opens the gate dialog
  with the override reasons), each column's menu moves its trips at once (dry run -> preview counts -> run; with a
  board filter on, only the cards on screen), cards show `waiting_on`, the quiet flag and the Stage 2 percentage, and
  the Closed group sits under the board. Every move, bulk move and auto-sent form shows the Undo toast for 10 s
  (`components/pipeline/UndoToast.tsx`, the API's Undo routes). The trip page: the stage menu, the gate dialog,
  Cancel trip (owner / agent; reason -> dry-run effects -> type CANCEL; no Undo), the Traveller info panel (s1 / s2 /
  s3, missing answers, [Send planning form] with the link shown once to copy, [Fill in from call]), the timeline with
  override, date-rule and undo reasons. C-01 shows the API's page grouping as three sections on one page (it lives in
  one iframe on agency websites) and focuses the first refused field; the call-slot picker lists the free starts in
  the visitor's own timezone. C-02 (`web/app/c/s/[token]`, client host, Referrer-Policy no-referrer): the browser
  reads `/crm/public/v1/s/{token}` on its own origin (the API's limit keys on the visitor, the web server never calls
  the API with the token) and renders the paged planning form (`components/intake/PlanningForm.tsx`, shared with
  Fill in from call; branches in `web/lib/planningForm.ts`). Settings > Forms: timezone, auto-send, call windows.

## CRM-16 agency-line ledger + suppliers (AA-944)

Design v3 §6.3 CRM-16 (M6 non-AA, M15, R17, R18), lane §2.7 / §3.1 / §3.2; migration crm-014. Routes in
`app/routers/ledger.py`, services in `app/ledger/` (`suppliers.py`, `lines.py`, `fx.py`); decisions in
`docs/implementation-notes/AA-944.md`.

- **Suppliers** (`/crm/v1/suppliers`, A-13): Adventure Asia is one built-in row per agency (`crm.ensure_aa_supplier()`,
  called by `provision_agency` and the list; never changed or trashed: 409 `aa_supplier_builtin`); own suppliers with
  vault-encrypted contact name / e-mail, revealed one field at a time with a reason (`POST .../reveal`, one `reveal`
  event); one `list_view` event per list. `default_commission_pct` is a margin key (stripped for the assistant) and
  needs `money_fields_edit` to set. A supplier that live lines name is not trashed (409 `supplier_in_use`).
- **Lines** (`/crm/v1/trips/{id}/lines`, `/crm/v1/lines/{id}`, the trip's Bookings tab): status planning -> confirmed
  -> cancelled. Confirm (`POST .../confirm`) runs `crm.booking_line_confirm` (SECURITY DEFINER): the agency's level
  (Settings > Payments: off / basic default / strict) decides the required fields (409 `line_incomplete` + `missing`),
  the money is frozen at the rate of the confirmation day in the agency timezone (R17), strict refuses a second
  confirmed line of one supplier with one number (409 `duplicate_confirmation_no`, card R), and the assistant is never
  blocked by a missing commission: the line is confirmed and the owner gets the task `add_commission` (R18). AA lines
  are the Booking feed's (CRM-19): 403 `aa_line_read_only` for members. Only a planning line goes to the trash (kind
  `booking_line`); a confirmed one is cancelled (hidden from travellers, R22). Agency lines never block a trip delete.
- **Money** (`PUT /crm/v1/lines/{id}/money`, owner / agent): cost, sell, commission_expected, each a new row through
  `crm.booking_line_money_put` (frozen rows never change: card E); responses carry money as keyed objects (`cost`,
  `sell`, `commission_expected`, `margin_base`) that the margin serializer strips; the database returns no money row
  to the assistant (crm-014 RESTRICTIVE policy), so lines and the ledger have no `money` block for her.
- **Ledger** (`GET /crm/v1/bookings`, A-11): every live line of live trips (filters status, from, to; 500 max), one
  `list_view` event; owner / agent also get base-currency totals: confirmed sell, cost, margin, expected commission from
  frozen amounts, planning sell as pipeline value at today's rate (lines without a rate counted apart).
- **FX** (`app/ledger/fx.py`): `crm.fx_resolve` decides (override of the day -> platform of the day -> latest earlier
  day within 30 days, `stale_days`; 409 `fx_rate_missing` on a confirm without any). Settings > Payments
  (`/crm/v1/settings/payments`, owner): base currency (default USD), validation level, FX mode (manual = overrides
  only); FX overrides (`/crm/v1/fx/overrides`, owner, append-only). A platform rate shown carries the attribution
  "Rates By Exchange Rate API"; no rates feed is exposed.
- **waiting_on**: hook `supplier_lines` (priority 50, AA-943 contract) = `supplier` while a booked / pre-departure trip
  has a planning agency line; registered when `app.ledger.lines` is imported (the router does).
- **Web**: the trip workspace's Bookings tab (`components/ledger/LinesPanel.tsx`: Day | Item | Supplier | Conf no |
  Status | Cost (owner / agent) | Sell | FX (rate @ date) | Collect; add, money, confirm, cancel, delete), `/bookings`
  (A-11, status filter, totals for owner / agent), `/suppliers` (A-13, AA pinned, contact behind a reveal with a
  reason), `/settings/payments` (owner; others see why). Refusal codes are worded in `lib/ledger.ts` / `lib/i18n.ts`
  (EN default, VI); the BFF lines are in `lib/bff.ts` (card AA-944). Dev E2E: `e2e/tests/ledger.spec.ts`.

## CRM-17 Module 5 e-mail (AA-945)

Design v3 §6.3 CRM-17 (M5, R24, Q2 4.1), lane §2.8 / §6.5, **as changed by the BYO errata S247** (ADR 0010 decision 9):
send-as through the agency's own **Gmail, Microsoft 365 or SMTP** (send only), `allow_platform_sender` **removed**, sender
kinds `agency_mailbox | system` only. Migration crm-015; code in `app/mail/`, routes in `app/routers/mail.py`; decisions
in `docs/implementation-notes/AA-945.md`. AA-CIS-Infra `crm_mail.tf`: MX `crm-in.<zone>`, the SES receipt rule
`aa-crm-dev-ingest` in the region's one active set (after `e2e-cognito`), the task-role grants on the ingest bucket,
secret `crm/<env>/mail-oauth`.

- **Receive (forward-in)**: an agency forwards or BCCs to `<local_part>@<CRM_INGEST_DOMAIN>` (`GET /crm/v1/mailbox/ingest-address`,
  created on first read as `<slug>-<16 hex>`, 64 random bits). AA's SES stores the raw mail under `raw/` (bucket default SSE-KMS, <= 1
  day); `crm_mail_ingest` parses it (stdlib `email`), finds the agency by `ingest_tx`, threads it (`app/mail/threads.py`:
  In-Reply-To / References hash -> `[ref:<trip mail_token>]` in the subject or `+tag` (card E) -> the sender's blind
  index (the traveller e-mail index) -> Unmatched), stores subject / text / HTML / From / To / the Message-ID under the
  tenant `mail` DEK, keeps PDF / image attachments of at most 7 MiB as documents (`uploaded_by_kind = mail`, only when
  the conversation has a client or trip; else a note code), and deletes the raw object. A Message-ID already stored is
  a duplicate (BCC + forward). Google's forwarding confirmation code goes to the address (shown in Settings > Mailbox).
  A copy of the agency's own sent mail (From = one of its mailboxes **and** SES's verdicts PASS with DKIM / DMARC
  aligned to the From domain, read from SES's own topmost `Authentication-Results`) is stored as a sent outbound
  message; a From that is only claimed is stored inbound. The recipient comes from SES's own topmost `Received ... for`
  line when it has one. A parser error deletes the object; a store error keeps it for the next run until it is 1 hour
  old, then it is dropped (`failed_permanent`); the job pages past failing objects so they never starve new mail.
- **Send (send-as)**: `POST /crm/v1/messages` seals the draft and queues one dispatch row (`message_id`, 10 s Undo);
  the dispatcher's gate runs, then `app/mail/sender.py` (the crm-010 `Sender`) builds the MIME message (trip tag in the
  subject, In-Reply-To the client's latest mail, References ending with the CRM's own Message-ID: Gmail's API replaces
  the Message-ID and the client's reply still threads by References, S252) and sends it through the member's own mailbox, else the agency's
  (`app/mail/providers/`: Gmail `users.messages.send`, Graph `me/sendMail` MIME, SMTP with TLS required on ports
  25 / 465 / 587 / 2525, the host resolved once and the socket opened to that vetted public address (no DNS
  rebinding into the VPC); stdlib `urllib` / `smtplib`, no new dependency). A refresh token the provider rotates
  (Microsoft: on every refresh) is sealed again in place. A send that keeps failing until the dispatch job's last
  attempt fails the message (`retries_exhausted`) and drops the row `send_failed`. A dead token or refused login -> `held_reconnect` (message and dispatch
  row), mailbox + credential `disconnected`, one AA system mail alert to the owners; **never** sent another way (card
  R). Reconnecting (OAuth or SMTP) or a passing health check re-queues the held rows. The sender table
  (`app/mail/senders.py`, `GET /crm/v1/mail/senders`) lists every send path; a unit test fails when one is missing.
- **Mailboxes**: own (every role, `OWN_MAILBOX_CONNECT`) or the agency's (owner, `INTEGRATIONS_MANAGE`); OAuth with
  PKCE, the state sealed under the agency's mail DEK and bound to the member (`app/mail/oauth.py`, 10 minutes, no
  server-side store); SMTP is logged in once before it is stored, and only after the caller's membership and
  permission for the scope were checked (the check dials a host the caller names). Credentials are `crm.integration_credential` per
  member (crm-015); Disconnect deletes the credential and keeps the mailbox row.
- **Work**: conversations by trip / client / Unmatched (`GET /crm/v1/conversations`, ids and counts only, one
  `list_view`), a thread decrypted for the agent (one `view`), link an Unmatched thread to a client (`CLIENT_EDIT`),
  drafts autosaved by an id the composer picks (`PUT /messages/drafts/{id}`; only the author; crm-015 lets crm_app
  delete drafts only), Undo within 10 s, a held / failed message back to a draft (`POST /messages/{id}/edit`), saved
  replies (agency-wide: owner via `WORKFLOW_EDIT`; own: every role), the mbox / CSV history import
  (`POST /crm/v1/mail/imports`, raw body, 25 MB; one job per 500-message slice, each slice queues the
  next, so sends and forwarded mail run between slices; the last failed attempt marks the import `failed`). S252: the
  web's Settings > Mailbox "Import mail history" sends the file through the BFF raw route `POST /bff/mail/imports`
  (bytes unchanged, capped at 25 MiB while it streams in) and polls `GET .../imports/{id}`; `/bff/mail/imports*` is
  outside the proxy matcher (`web/proxy.ts`) because Next cuts a proxied body at 10 MiB without an error.
- **Web**: Settings > Mailbox (`components/mail/MailboxSettings.tsx`: connect, SMTP, Test, Disconnect, who pays, the
  forward-in address and Gmail code, the Microsoft 365 BCC help, the sender table), `/connect/mail/callback` (no side
  effect: hands code + state to Settings > Mailbox, which completes through the BFF), the trip's Messages tab
  (`MessagesPanel.tsx`: HTML only in `<iframe sandbox="" srcdoc>` behind a CSP `<meta>` that blocks every remote
  load, autosave every 3 s, Undo, the held chip), `/inbox`
  (Unmatched, link to a client), Settings > Saved replies, the red banner when a mailbox is disconnected.

## CRM-18 thin full lifecycle (AA-946)

Design v3 §6.3 CRM-18 (M4 base, M7 manual, M11 base, M12 v0); migration crm-016; code in `app/proposals/`
(`service.py`, `public.py`, `agreements.py`), `app/portal/` (`access.py`, `public.py`, `progress.py`), routes in
`app/routers/proposals.py` (`/crm/v1`) and `app/routers/client_pages.py` (`/crm/public/v1`); decisions in
`docs/implementation-notes/AA-946.md`.

- **Agreements** (Settings > Agreements, owner `TERMS_PUBLISH`): booking terms and waiver versions; publishing retires
  the published one of the kind; a published version never changes (DB CRM31), its SHA-256 is the database's.
- **Proposal** (the trip's Proposal tab): a text version with the client's sell amounts (currency, total, deposit,
  balance date) and the terms version; draft (every role) -> send (owner / agent, trip at Proposal): `price_hash`
  fixed, earlier sent version superseded, a `proposal` link for the lead through the dispatcher, path returned once
  ("Copy link") -> the client accepts on C-03/C-04 (`/c/p/<token>`: typed full name, terms tick, the same price hash)
  -> deposit + balance schedule rows, a `collect_deposit` task.
- **Mark paid** (owner / agent `PAYMENT_MARK_RECEIVED`): one append-only `crm.payment`, the row paid, a
  `trip_commitment` payment (delete block); the deposit moves a trip at Proposal to Booked (`deposit_paid`, the
  computable booking gate) and invites every traveller to the portal. Void a due balance row (a deposit row is never
  void: 409 `deposit_not_voidable`; a deposit taken outside the CRM is marked paid with its method).
- **Portal base** (`/c/t/<token>`, one link per traveller, `client_link` purpose `portal`, 30 days, re-issue revokes):
  a 6-digit e-mail code (10 min, 5 tries, 5 an hour) sent from the agency's own mailbox (trip owner's, else the
  agency's; none -> 409 `code_delivery_unavailable`; `PORTAL_OTP_DEV_ECHO` on Dev returns it) opens a 12-hour session
  cookie. The stored code mail shows the code masked (`app/portal/codes.py`); the sender fills it in from
  `otp_challenge.code_enc` at send time and clears it, so no agency member can read a code in the Messages tab (the
  provider's Sent copy is the v0 limit below); the hourly date-rule run expires a pending code past its 10 minutes and
  drops its sealed copy. A `sign` code is 403 `stage3_locked` while signing is locked. Home: own Stage 3 progress (codes, never values), the waiver, for the lead the party's progress and the
  payment schedule. **Stage 3** (the F1 identity fields, write-only) and **signing** are 403 `stage3_locked` until the
  deposit is paid or the trip was booked with the `manual_booking` override (`crm.portal_unlocked`; DB CRM32 too). **E-sign v0**: published waiver + its hash, typed name, consent tick and a fresh
  signing code -> `signature_request` signed with a `trip_commitment` signature; the row keeps (sealed) the code's
  `code_delivery` and the canonical evidence record (no PII: the typed name only as its SHA-256) whose SHA-256 is
  `evidence_sha256` (DB CHECK), so the evidence re-verifies after the 24 h code purge.
- **E-sign v0 limit (known, re-review S251; the fix is Nghiệp's decision):** the code mail goes out from the agency's
  own mailbox (errata S247: never a platform sender) and Gmail / Microsoft / SMTP keep a **Sent copy** that the owner of
  that mailbox can read, and the same staff are shown each traveller's portal link. So `code_delivery = mailbox`
  proves "sent to the traveller's address from the agency's mailbox", **not** "only the traveller saw the code": the
  agency mailbox owner could sign in and sign as a traveller. Options in `docs/implementation-notes/AA-946.md`
  (Microsoft `saveToSentItems=false`, Gmail delete the Sent copy, an SES exception for code mails only, or no portal
  links shown to staff). Until then a v0 signature is agency-attested evidence, not proof of the traveller's sole
  control.
- **Stage 3 gate**: s3 = done (traveller x [details, waiver]) / all, stored on `trip.intake_completeness.s3`; 100
  opens booked -> pre_departure (API and DB); the `waiting_on` hook `portal_stage3` (60) answers `client` for a booked
  trip below 100 whose travellers have portal access.
- **Web**: the trip's Proposal tab (`components/proposals/ProposalPanel.tsx`), Settings > Agreements (`components/settings/AgreementsView.tsx`), the client pages
  `/c/p/[token]` (`components/proposals/ProposalPage.tsx`) and `/c/t/[token]` (`components/portal/PortalPage.tsx`; the
  session cookie never reaches page script; only the `client.*` strings ship, `lib/clientMessages.ts`), BFF lines card
  AA-946 in `lib/bff.ts`. Dev E2E: `e2e/tests/lifecycle-thin.spec.ts` (portal steps need `PORTAL_OTP_DEV_ECHO`).

## CRM-19 B2B client + AA lines (AA-947)

Design v3 §6.3 CRM-19 (M3 supply, M6 AA lines, R17, R18, R23; checklist: M2M client, catalog cache, lines from quote /
hold / feed only, "from"-price warning, AA-aware Lost reasons, outage queue); migration crm-017; code in `app/booking/`
(`client.py`, `contract.py`, `aa_lines.py`, `sync.py`, `views.py`, `products.py`, `checker.py`) and `app/catalog/`
(`client.py`, `store.py`, `sync.py`), routes in `app/routers/aa.py`; decisions in `docs/implementation-notes/AA-947.md`.

- **M2M client**: `booking.b2b.v1` 1.1 and `catalog.http.v1` 1.2 over the aa-m2m pool with one `TokenProvider` for the
  process (one cached token per scope; the scope each call needs only). Booking calls carry `X-AA-Tenant-Id` (Booking
  checks its `b2b_partners` row; an agency AA has not activated is 409 `aa_partner_not_active` / a failed request
  `unknown_partner`). Answers are read tolerantly (unknown fields and event types never stop the CRM).
- **Lines from quote / hold / feed only** (card R): the agent asks, never types. `POST /crm/v1/trips/{id}/aa-lines`
  (tour from the catalog cache, a Booking component, date or a fixed departure, pax counts, quote currency, owner /
  agent: a sell amount) -> `crm.aa_line_request()` creates the line `planning` / "AA pending" titled from the cache and
  the quote request; `POST /crm/v1/lines/{id}/aa/{hold|confirm|release|requote}` queue the asks the line's `aa.actions`
  list (confirm: a held line once the trip is booked or later; release: anything AA has not booked). Editing, money,
  cancel, confirm and trash of an AA line stay 403 `aa_line_read_only` (route, crm-014 guard, money functions).
- **Outage queue** (card E): `crm.aa_request` rows are sent by `crm_booking_sync`; a transient failure keeps the row
  pending (attempts, `next_attempt_at` 30 s doubling to 15 min, `last_error_class`) and the line reads "AA pending" with
  the next try; no duplicates: one live request per (line, kind), stable `Idempotency-Key` per request (and quote
  version), a quote owns its one line, a repeated quote writes no new money head. A hold answered `price_changed` is
  re-quoted once and held at the new price. Final refusals become the line's sub-status / `aa_result_code`. When
  Booking gives no answer at all (timeout, unreachable, 5xx, token pool down) the rest of the agency's batch goes back
  unsent and the feed waits: one timeout per agency and run. An answer is applied to the line as it is under the lock,
  not as claimed: a hold that lands after its line was released keeps only its hold id and queues the release.
- **Money of AA lines**: cost = the quote's `b2b_price`, sell = the agency's, frozen at once (`aa_quote_lock`,
  `quote_lock`; a non-USD base crosses USD through `crm.fx_resolve`). Only the job writes it.
- **Feed**: per agency with a `crm.booking_feed_state` row, `GET /events` pages into `crm.inbox_events` (each event in
  its own transaction, a replay applies nothing): `hold.expired`, `hold.extended` (refresh), `line.confirmed`,
  `line.declined` (cancelled / declined + an urgent `aa_line_declined` task), `booking.cancelled`; `departure.*`
  recorded only (the incident banner is a later card).
- **"From"-price warning**: `GET /price-parity` in the quote currency for a whole-tour line; `below_from_price` = sell
  per person (adults + children) below it. Warns, never blocks (ADR 0005a decision 1); the add form warns before
  asking too (`GET /crm/v1/aa/products/{source_id}?currency=`).
- **AA-aware Lost**: `GET /trips/{id}/lines` answers `lost_reason_hint` (declined -> `aa_on_request_declined`, or
  `aa_sold_out` when Booking said sold out); the Lost picker pre-selects it. Lost (and cancel) holds the waiting
  quote / requote / hold / refresh requests back by the Undo window and queues a release of every line AA has not
  booked, sent after the window; Undo cancels the release and the waiting requests then run as asked; without an Undo
  the sync drops them (`trip_closed`, Booking not called). AA
  bookings are left to the cancellation card (CRM-27, Booking's fee) and listed in the cancel dry run.
- **Catalog cache**: `crm.aa_catalog_tour` (platform, no tenant), refreshed hourly; `GET /crm/v1/aa/catalog?q=`
  searches served tours; a retired tour's lines read "no longer offered".
- **Web**: the Bookings tab (`components/ledger/LinesPanel.tsx`: AA status, retry note, warning, the listed asks) and
  "Add AA item" (`components/ledger/AaItemForm.tsx`), helpers `lib/aa.ts`, BFF lines card AA-947 in `lib/bff.ts`, EN
  default with VI strings; the trip page passes `lost_reason_hint` to the Lost picker. Dev E2E:
  `e2e/tests/aa-lines.spec.ts` (needs the AA-973 seed tour in `E2ECRMAATOUR`, a Booking partner row for the synthetic
  agency and a per-person price hint on the seed plan; card E stays in the integration tests).

## CRM-21a proposal model + day-list builder (AA-949)

Design v3 §6.3 CRM-21a (M3 agent side, spec rule 5, §11.2 concurrent edits, R33, R38 slot; Fix B10, E1, A5 / D1);
migration crm-019; code in `app/proposals/` (`builder.py`, `rules.py` pure, `aa_options.py` the live quote), routes in
`app/routers/builder.py`; decisions in `docs/implementation-notes/AA-949.md`.

- **A builder version is a `crm.proposal` row with `kind = builder`** (AA-946's text versions are `kind = text`); its
  days (`proposal_day`, day 0 = "Before you go", IANA `tz` per day), option blocks (`option_block`, default option,
  "client may swap" on by default), options (`option`: agency items, or AA items priced by a live Booking quote) and
  rules (`option_rule`, requires / excludes: the Advanced section). Picks (`selection`) exist only on a published or
  sent version (CRM-21b writes them through `builder.record_pick`).
- **Skeleton from Stage 1/2** (`POST /crm/v1/trips/{id}/builder`, every role): one day per date (or nights + 1), day 0,
  a Lodging block per night, the day zone from the destination, the rooms from the party (the agent edits them, Fix
  B10), 30 % deposit; the lodging default follows the lead's Stage 2 accommodation tier until the agent picks a
  default by hand (`option_block.default_pinned`: then no add / edit / delete moves it). Or a working copy of an
  earlier version (`from_proposal_id`). One builder draft per trip.
- **Every write sends the `edit_seq` it read**: another value is 409 `version_conflict` (`reason: edited`). Publish of a
  working copy whose base version got client picks since the copy is 409 `version_conflict` (`reason:
  client_selected`, the picks listed) until the agent sends `acknowledge_selection_seq`; the picks then carry over
  (`carried`). A rule the defaults (or a pick) break is 422 `option_conflict` (card R).
- **Publish vN** (`PROPOSAL_PUBLISH`, owner / agent): checks (a default per block, none sold out, rules hold, something
  to charge, a balance date exactly when a balance is left, AA quotes valid, every AA item priced), the amounts from the defaults (pay-on-arrival items listed, never charged),
  the price hash, status `published` (one per trip, the previous one superseded). The database keeps a published
  version's rows unchanged (CRM35) except an option's availability. Sending it (the C-03 link) is CRM-21b (AA-950):
  `sent_at` is set-once so a published version can still be sent.
- **AA items**: the drawer searches the catalog cache and Booking's product look-up (AA-947 routes), then
  `POST .../blocks/{id}/aa-options` quotes live (`proposal_ref crm-proposal:<id>`, the version, `line_ref` = the
  option) between two transactions; net = b2b price, availability from the quote, sold out cannot be added, Booking
  down = 503 and nothing stored. A hotel room needs its nights (`option.aa_nights`, 422 `invalid_nights`). Without a
  sell in the request the sell stays 0 (never the b2b price: the assistant reads the sell) and Publish waits for an
  owner / agent to price it (`aa_item_unpriced`). "Refresh AA prices" re-quotes with the same nights, the oldest quote
  first, 40 a run; an item left over is reported `refresh_limit` (`refresh.skipped`), an undated one
  `day_has_no_date`. No hold, no AA line, no money row: those come after the client accepts (CRM-21b).
- **Roles**: every role builds; sell / net / deposit share need `MONEY_FIELDS_EDIT`; net and margin are built only for
  owner / agent (and stripped by the serializer).
- **Web**: `/pipeline/<trip>/proposal/<proposal>` (`components/proposals/BuilderPage.tsx`, the drawer
  `BuilderAaDrawer.tsx`, helpers `lib/builder.ts`), started from the trip's Proposal tab (section "Day-by-day proposal");
  BFF lines card AA-949; EN + VI. Dev E2E: `e2e/tests/proposal-builder.spec.ts`.

## CRM-21b client proposal + accept + hold (AA-950)

Design v3 §6.3 CRM-21b (M3 client side, R4, R19, R26, C31; Fix A3, B9, B11; cards R / E); migration crm-021; code in
`app/proposals/` (`client_page.py` the choice and the client view, `holds.py` the hold set, `events.py` events / bell /
nudge, `public.py` the client page), `app/proposals/service.py` (send of a builder version, re-issue, client
activity), routes in `app/routers/client_pages.py` and `app/routers/proposals.py`; decisions in
`docs/implementation-notes/AA-950.md`.

- **Send** (`PROPOSAL_PUBLISH`): a published builder version -> sent; the version sent before is superseded. When its
  link is live (same lead, 3+ days left) the new version takes over that link (`link_reused`, no path returned) and the
  client's page shows one bundled notice of what changed; otherwise a new link, its path returned once.
  **Re-issue link** revokes the link (every device loses it) and moves every version on it to the new one.
- **Link cookie (Fix B11)**: `/c/p/<token>` (web route handler) sets `aa_proposal_link` and redirects to `/c/p`; the
  page calls `/crm/public/v1/proposal` with same-origin credentials. The token stays valid until it expires: a second
  device that opens the link gets in (card E). `/p/{token}` GET / accept stay for old links.
- **C-03**: one option per block (the pick, else the default; a sold-out pick falls back), the sell difference per
  option (D1; nothing for `total_only`), confirmation classes (R26), rule clashes greyed with the reason, "Choose
  later" = the default marked for later, agency items only (Fix B9). Never a net, margin or per-line price.
- **Hold set (Fix A3)**: one Booking hold per version covering the chosen AA options (one multi-line quote, then
  `POST /holds`, `Idempotency-Key crm-proposal:<id>:v<N>:a<attempt>`: a fresh key per new request, since Booking
  refuses a used key with another quote), reused while it covers the choice and is live; every later hold is a
  re-check: at most 3 a UTC day per version, the 4th refused before Booking (429 `recheck_limit`, "Ask your agent");
  a re-hold rings the trip owner's bell. The old hold is released only after a clean quote; when it was released and
  no new hold came, the row's hold columns are cleared (never a hold Booking no longer has); when the release itself got
  no answer the hold moves to `hold_release_id` (not shown, not accepted) and the next request releases it again
  first (a no-op when done), so Booking never keeps a second set for the version. A hold stored on the row (a replay too)
  clears `hold_release_id`: Booking made it only once no other set of the version was active. Booking's quote leaves the
  version's own live hold out of its seat count (AA-Booking, AA-950), so seats this client holds never read as sold
  out to them. The request in flight
  (`hold_pending_*`, written before `POST /holds`) is replayed with the same key and quote when its answer was lost,
  which recovers the hold Booking made (before any reuse of the row's hold: that request may have released it); a
  replay that stores no hold (refused, gone, no answer) moves the row's hold to `hold_release_id` for the same reason,
  so it is never answered "held" or sealed by accept; another screen meanwhile gets 409 `hold_in_progress`. A chosen option Booking
  answers sold out -> marked sold out, the pick dropped, 409 `option_sold_out` with the fresh page (card E); a choice
  that then breaks an agency rule is neither held nor accepted (409 `option_conflict`). Booking down -> 503, one bell
  an hour.
- **Accept**: price hash, terms, rooms confirmed (Fix B10), `selection_hash` (409 `choice_changed`), the agency's rules
  (409 `option_conflict`), no hold request of the version in flight (409 `hold_in_progress`: another screen may be
  releasing the hold this page shows; `hold_required` when that request lost its answer), a live hold over
  the AA choice (409 `hold_required` / `hold_expired` + `requote`, card R); the schedule follows the accepted picks; the
  `collect_deposit` task is due at the hold expiry (C31). AA lines from the accepted hold set: CRM-23 (AA-952) below.
- **Agent side**: the lifecycle list carries viewed / hold / accepted total; `GET /proposals/{id}/client-activity`
  (R19 choices, events); `GET /bell`, `POST /bell/seen` (the caller's unseen `crm.metric_event` items, bundled per
  proposal and event). R4 nudge: the hourly `crm_stage_date_rule` gives the owner one `proposal_follow_up` task and a
  bell item 2 days after the first view without a pick.
- **Web**: `app/c/p/[token]/route.ts` (exchange), `app/c/p/page.tsx`, `components/proposals/ClientBuilderProposal.tsx`
  (phone first, 390 px), `components/proposals/BuilderClientRow.tsx` (Send / Re-issue / viewed / choices),
  `components/BellMenu.tsx` (top bar), `lib/clientProposal.ts`, `lib/bell.ts`; BFF lines card AA-950; EN + VI. Dev E2E:
  `e2e/tests/client-proposal.spec.ts`.

## CRM-23 AA lines from the accepted hold set + confirm after the deposit (AA-952, C-M3 "confirmed")

`app/booking/proposal_lines.py`, run by `crm_booking_sync` (`app/booking/sync.py`, `BookingSync.proposal_sets`, before
the request queue; accept and a paid deposit nudge it). No migration.

- **Lines**: an accepted builder version with `accepted_hold_id` and no AA line of its options gets one AA line per held
  option, written by the job (the client link never writes an AA line: crm-014 CRM24). **The line id is the option
  id** = Booking's `line_ref`, so the confirm answer and the feed events name the line without a mapping. Booking's
  `GET /holds/{id}` (`booking.b2b.v1` **1.2**: the held quote's `price_hash`, `currency`, `fx`, each line's
  `b2b_price`) gives the net (cost, the quote's FX lock, `crm.booking_line_aa_money`); the sell is the option's (the
  agency's price). `bk_hold_id` = the accepted hold, `aa_price_hash` = its quote's hash, `aa_quote_version` = the
  version. Booking says expired / released -> lines `expired` + urgent task `aa_hold_expired` (re-quote); unknown hold
  -> lines `failed` (`hold_not_found`) + task `aa_lines_failed`; a 1.1 answer -> nothing written, retried next run.
- **Confirm**: once the accepted proposal's deposit row is `paid` and the trip is booked / pre-departure / travelling,
  one `confirm` request (`crm.aa_request`) on the set's first line, only while the hold is live and no confirm of the
  set was ever queued; sent with `Idempotency-Key crm-confirm:<request id>` (stable on retries; Booking also answers a
  confirmed hold with its booking). A confirm, its refusal and a release act on the whole hold, so `apply` writes them
  on every line sharing the hold in the trip (`SET_LINES_SQL`). A refused confirm -> lines `failed` (or `expired` /
  `sold_out` / `not_served`) with Booking's code + urgent task `aa_confirm_failed`; not re-sent by itself; the trip
  stage never moves back. Money stays behind the margin serializer: the assistant sees the lines, never cost or sell.

## CRM-23 agency invoices (AA-952 slice 2)

Design v3 group F (M7, R27), A-10 Receivables, the trip's Invoices card; migration crm-024; code in `app/invoices/`
(`service.py`, `render.py`), routes in `app/routers/invoices.py` (`/crm/v1`); decisions in
`docs/implementation-notes/AA-952.md` (slices 2 and 3).

- **From a schedule row** (owner / agent `PAYMENT_MARK_RECEIVED`): a due deposit / balance row of the accepted proposal
  -> a draft with one charge line of the row's amount and currency (always tax-inclusive: it bills what the client
  accepted) and "included" lines = the titles of the trip's live agency and AA booking lines, no price (design §2.9
  rule 3). One live invoice per row (409 `invoice_exists`; a void one frees it).
- **Typed** (`adjustment`, or `service_fee` when Settings > Payments turned service fees on, 409
  `service_fee_disabled`): charge lines quantity x unit amount, the agency's tax setting (inclusive or on top).
- **Draft -> issue -> void**: a draft is edited (memo, due date, tax; a typed one's lines); issue takes the agency's next
  number (`<prefix>-<6+ digits>`, `crm.invoice_sequence` +1 under the row lock: gapless), today in the agency timezone,
  a due date never before it (default + `invoice_due_days`); the database checks the lines against the totals (CRM39)
  and seals the invoice (CRM31; 409 `invoice_not_draft`). Void a draft or an unpaid invoice with a reason (409
  `invoice_has_payments` / `invoice_not_voidable`). The trip's `crm.trip_commitment` row (kind invoice) follows the
  status (a paid one blocks the trip delete).
- **Document** (`GET /invoices/{id}/document`, every role): JSON `{html, filename}`; the HTML escapes every value and
  carries a CSP `<meta>` (inline styles only: no script, no remote load). The agency's display name heads it; the
  bill-to is the trip lead's display name, read through the vault at render time (no name is copied into an invoice).
  The web opens it in a new tab (print / save as PDF).
- **Receivables** (`GET /invoices`, every role): issued and unpaid by default, by due date, overdue flagged; status
  filter. Settings: `GET / PUT /settings/invoices` (owner `PAYMENT_SETTINGS`): prefix, tax label / rate / inclusive,
  due days, service fees.
- **Web**: the trip's Invoices card (`components/invoices/InvoicesPanel.tsx`), A-10 `/payments`
  (`app/(crm)/payments/page.tsx`), Settings > Payments "Invoices" (`components/invoices/InvoiceSettingsForm.tsx`);
  helpers and refusal wording in `lib/invoices.ts`; BFF lines card AA-952 in `lib/bff.ts`. New forms use the kit form
  controls (`Field`, `Input`, `Select`, `Textarea`, `Checkbox`, now in the `app/_kit` barrel).

## CRM-23 payments, refunds and payment methods (AA-952 slice 3)

Design v3 group F (M7), §4.6 (refund approval), §6.3 (BYO providers), A-10; migration crm-024; code in `app/payments/`
(`service.py`, `providers.py`), routes in `app/routers/payments.py` (`/crm/v1`); decisions in
`docs/implementation-notes/AA-952.md`.

- **The agency is always the merchant.** Money moves between the client and the agency's own account (its bank, cash
  desk, own card terminal, later its own provider account). No Adventure Asia account holds, receives or refunds
  client money; the CRM records what moved.
- **Record a payment** (`POST /invoices/{id}/payments`, owner / agent `PAYMENT_MARK_RECEIVED`) on an issued invoice:
  amount (partial payments; 422 `amount_above_balance`), method, the day (today or earlier), the agency's own
  reference (never card or bank numbers). One append-only `crm.payment` row (provider `manual`, `invoice_id`, the
  invoice's schedule row), its `trip_commitment` (payment succeeded). The payments reaching the total make the
  invoice paid (commitment invoice paid) and a deposit / balance invoice settles its schedule row through
  `app.proposals.service.settle_schedule` (the CRM-18 effects: Proposal -> Booked, portal invites, the Booking
  confirm). The schedule's own "Mark paid" is refused while an issued invoice bills the row (409
  `invoice_issued_for_row`). Every invoice view carries `payments`, `refund_requests`, `paid_amount`,
  `refunded_amount`, `balance_amount` (total - payments; a refund never reopens an invoice).
- **Refunds** only from an approved `crm.refund_request`: proposed (owner / agent, never more than is left to ask back
  on the payment: 422 `amount_above_refundable`) -> approved / rejected (`REFUND_APPROVE`: the owner any amount, an
  agent up to `tenant_settings.refund_owner_threshold_base` on the amount converted to the base currency with the
  ledger's FX resolution; no rate = only the owner; 403 `above_threshold`) -> executed (`PAYMENT_MARK_RECEIVED`): the
  payment's provider refunds (manual: the member records the money returned) and a refund entry (`entry_kind`
  refund, `refund_of`) joins `crm.payment`; a fully refunded payment's commitment reads `refunded` (still
  `has_money`). Every money step of an invoice runs under its row lock.
- **Payment methods** (`/settings/payment-methods`, owner `PAYMENT_SETTINGS`): the manual method's name and pay-by
  instructions (printed on the agency's unpaid invoices), disconnect. `app/payments/providers.py` is the BYO
  abstraction (`PaymentProvider`: `refund`, `takes_cards`, `needs_external_account`); only `manual` has code, `stripe`
  (Connect Standard on the agency's own account) and `vn_gateway` answer 409 `provider_not_available`.
- **Web**: `components/invoices/InvoiceMoney.tsx` (record payment, payments, request refund) and `RefundActions.tsx`
  (approve / reject / mark refunded) on the trip's Invoices card; `RefundsPanel.tsx` on A-10 `/payments`;
  `PaymentMethodsForm.tsx` on Settings > Payments; BFF lines card AA-952 in `lib/bff.ts`.

## CRM-22 text proposal + Before you go (AA-951)

Design v3 §6.3 CRM-22 (M4, R38, R9; F4 / WF-5 A-05; Q1 2.3); migration crm-022; code in `app/proposals/`
(`text_render.py` the pure renderers, `text_proposal.py` preview and send, `prep_packs.py` the day 0), `app/mail/templates.py`
(the library), `app/catalog/content.py` (the CIS content sync), routes in `app/routers/proposal_text.py`; decisions in
`docs/implementation-notes/AA-951.md`.

- **Text proposal** (`GET /proposals/{id}/text?format=email|whatsapp&locale=en|vi&template_id=`, every role): one version
  rendered from one snapshot (defaults, sell amounts, D1 deltas, rooms, days with zone offsets, the day 0, the price
  block; never a net or a margin) with the version's `price_hash` (reference = its first 12 hex digits, in every text).
  A builder version renders once published, a text version as a draft (its hash computed as Send fixes it).
- **Send** (`POST /proposals/{id}/text/send`, owner / agent; e-mail also `MESSAGE_SEND`): the body names the hash the
  preview showed; **another hash is 409 `price_hash_mismatch`** (card R) before any write. A version not yet sent goes
  out through the link send (AA-946 / AA-950) in the same transaction and the text carries
  `<first INTAKE_PAGE_ORIGINS entry>/c/p/<token>`; a sent version keeps its link unless the body asks for `new_link`
  (the link is re-issued into the text: the page does so after the agent undid the e-mail that carried the only copy
  of the link). E-mail = one message through the AA-945 composer (send-as, Undo; no mailbox = 409 and nothing is
  sent); WhatsApp = the final text returned for the agent to paste (no WhatsApp channel), audit `proposal_text_send`.
  `{{traveller_first_name}}` = the first word of the lead's display name (never the F1 `given_name`: reveal only).
- **Before you go** (day 0, R38): the builder view's `before_you_go` lists CIS prep packs whose `applies_to` names a
  country / tour of the version's AA options or the trip's live AA lines (**none without an AA item**, card E: no
  suggestion, a pick is 422 `no_aa_items`, no pack rendered); `PUT /proposals/{id}/before-you-go` (draft, `seq`) copies
  every locale of each picked group into `crm.prep_pack_selection` (a working copy copies them). Rendering keeps only
  the official `https://` links of a visa section (Q1 2.3), adds the insurance reminder, and the fitness warning when an
  AA tour is challenging / strenuous or a trek / high-altitude. Day 0's priced items are listed in the texts too. The
  client page C-03 carries the same section (`before_you_go` of `GET /crm/public/v1/proposal`,
  `prep_packs.client_view`; both go through `prep_packs.rendered`, the card E gate).
- **Templates** (R9, `GET|POST /message-templates`, `PATCH|DELETE /message-templates/{id}`,
  `POST /message-templates/import-aa`): owner writes (`WORKFLOW_EDIT`), every role reads; the import copies the CIS
  starter templates once per group and locale. Merge fields: the CIS seed names plus `{{proposal}}`.
- **Web**: `/pipeline/<trip>/proposal/<id>/text` (`components/proposals/TextProposalPage.tsx`), the builder's day 0
  (`components/proposals/BeforeYouGoPanel.tsx`), Settings > Message templates (`components/settings/MessageTemplates.tsx`),
  helpers `lib/textProposal.ts`, BFF lines card AA-951; EN + VI.

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
  TenantSummary: id, agency name, state, synthetic, has owner, holds travellers, created, and since crm.ops.v1 1.2
  `lock_source` (`aa_ops` / `cis_tenant` / `cis_membership` while locked) and the CIS feed's `membership_feed_state` /
  `membership_feed_lag_s` (seconds since the last good pull, platform-wide); the ids come from the crm-007
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
  (`app/lifecycle/booking.py`; AA-947 fills it with `app.booking.checker.BookingChecker`: `GET /holds/{id}` per hold);
  none configured = 503 `booking_not_configured`, Booking down = 503 `booking_unavailable`, the trip stays in the trash.
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
members given as `--member KEY:ROLE:SUB` (the subjects the AA-CIS-Infra e2e user script prints; issuer = partner pool;
no EXTERNAL fallback since AA-979 P2b, `--issuer` otherwise). Faker EN/VI travellers (`app/seed/travellers.py`,
deterministic, NFC, `.invalid` e-mail) are
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
`COGNITO_PARTNER_ISSUER`, `COGNITO_PARTNER_WEB_CLIENT_ID` (the retired `COGNITO_EXTERNAL_ISSUER` /
`COGNITO_CIS_PORTAL_CLIENT_ID` are ignored, AA-979 P2b),
and the key provider `KEY_PROVIDER` (`kms`; `local` only in test / local), `KMS_DATA_KEY_ID` (`alias/crm-<env>/data`),
`DEK_CACHE_TTL_SECONDS`, and the public intake (AA-936) `INTAKE_PAGE_ORIGINS` (the client host origin; empty on Dev =
every submission 503 `intake_not_configured`), `RATE_LIMIT_INTAKE_PER_MINUTE` (default 5 per address),
`RATE_LIMIT_INTAKE_FORM_PER_HOUR` (default 200 per form), and the documents bucket (AA-937) `DOCUMENTS_BUCKET`, the
audit export (AA-940) `AUDIT_BUCKET_COMPLIANCE` / `AUDIT_BUCKET_GOVERNANCE` (both or neither) and
`AUDIT_RETENTION_DRY_RUN` (default true), and (AA-939) `COGNITO_PARTNER_POOL_ID`, `PARTNER_WEB_ORIGIN`,
`SES_FROM_ADDRESS`, `SES_CONFIGURATION_SET`, `INVITATION_TTL_HOURS` (default 168), `RATE_LIMIT_CLAIM_PER_MINUTE`
(default 10), and the F3 guardrail (AA-938) `SCRUB_GUARDRAIL_ID`, `SCRUB_GUARDRAIL_VERSION`,
`SCRUB_GUARDRAIL_REGION`, `SCRUB_DETECTOR_TIMEOUT_MS`, and the data lifecycle (AA-942) `LIFECYCLE_DRY_RUN` (default
true), and the FX source (AA-944) `FX_PROVIDER` (`synthetic` by default on dev / test / local, `live` on stg / prod;
not set on Dev today), the CIS membership feed (AA-978) `CIS_API_URL` (the CIS API origin) and `M2M_CLIENT` (secret
`aa-crm/<env>/m2m-booking-client`, the JSON `{client_id, client_secret, token_url, scopes}`; either empty = the pull
logs `not_configured`; AA-947: the same client asks `cis.catalog/read` for the catalog cache and `booking.b2b/*` for
Booking), the Booking API (AA-947) `BOOKING_API_URL` (the Booking API origin; empty = AA requests stay "AA pending" and
the product look-up is 503 `booking_not_configured`), and mail (AA-945) `CRM_INGEST_BUCKET`, `CRM_INGEST_DOMAIN`, `MAIL_OAUTH_REDIRECT_BASE` and the
secret `MAIL_OAUTH` (`crm/<env>/mail-oauth`, mapped only once it has a value), and the portal (AA-946)
`PORTAL_OTP_DEV_ECHO` (default false; refused outside dev / test / local; on, the portal code answer carries the code
for the synthetic Dev agencies that have no mailbox). Meanings and defaults:
`api/app/settings.py`. Live check of the key provider inside the
container: `python -m app.vault.selftest` (KMS round trip under the nil tenant context, nothing stored).

## CIS membership feed (AA-978, CRM-P1)

Root contract `tenant.membership.v1` (vendored byte copy and samples in `docs/contracts/`, hash pinned by
`api/tests/unit/test_cis_contract.py`; the root repo is private, so CI cannot fetch it). `crm_membership_pull`
(`app/cis/membership_feed.py`):

- Pages of at most 200 events after the stored cursor (`crm.membership_feed_state`, crm-013), at most 5 pages a run;
  the cursor moves only from the value the run read (compare and set), so overlapping runs never move it back.
- Each event in its own `membership_feed_tx`: skipped when its `event_id` is already in `crm.membership_inbox`; else
  checked against the contract (`app/cis/contract.py`: closed objects, the enums; a `crm_role` anywhere is
  `crm_role_present`; a value of the wrong JSON type is refused, never raised) and applied, then recorded with its
  outcome (`applied` / `ignored` / `rejected`) and reason code in the same transaction. Rejected events change
  nothing. Only an agency the CRM holds (active or locked) gets an inbox row: events of unknown or offboarding
  agencies are ignored without one, so no row outlives its agency's erase.
- An event the database refuses for a reason of its own (SQLSTATE class `CR` or `23`) is recorded as rejected /
  `apply_refused` (ERROR log `crm_membership_event_apply_refused`) and the run goes on; any other error stops the run
  as `apply_error` with the cursor before that event (backoff 20 s doubling to 5 min).
- Rules: `membership.removed` disables the CRM member with the same `(iss, sub)` in that agency and signs them out of
  the partner pool after the commit (a withdrawn invitation has no identity: ignored `no_identity`). The feed takes
  the crm-008 per-agency owner lock first, so a concurrent owner demotion cannot leave the agency without an owner;
  if they were the last active owner the agency is locked with `lock_source = cis_membership` (only the owner
  recovery or an AA unlock reopens it). `tenant.locked` / `tenant.deleted` lock an
  active agency (`cis_tenant`); `tenant.unlocked` lifts only a `cis_tenant` lock. `tenant.created`, `invited`,
  `activated` and `role_changed` are recorded as ignored: no CIS event creates a CRM member or changes a CRM role.
  CRM-only members have no CIS row and are never touched.
- `crm.tenant.lock_source` (crm-013 trigger): a lock that names no source is `aa_ops` (the L1 console's lock, and
  every lock from before crm-013); leaving `locked` clears it; only the feed actor names a `cis_*` source (CRM21). An
  L1 lock on an agency the feed locked takes the lock over (`aa_ops`, audited `tenant_lock` with field
  `lock_source`), so a later CIS `tenant.unlocked` cannot lift it.
- Each applied change writes the agency's audit (`member_deactivate`, `tenant_lock`, `tenant_unlock`, actor
  `platform:feed-tenant-membership`, object id `cis_event:<id>`). Job results are counts and codes only.
- Monitoring: `/crm/health/ready` carries `membership_feed` (`ok` / `pending` / `stale` = no success for 10 minutes /
  `failing` / `not_configured` / `unknown`; never fails readiness), and from 10 consecutive failures every failed run
  logs the ERROR line `crm_membership_feed_stuck` (the CloudWatch metric filter and alarm key on it, AA-CIS-Infra
  `crm_membership_alarms.tf`, with a second alarm on `crm_membership_event_apply_refused`). The L1 agency summary
  carries the same word and the lag (crm.ops.v1 1.2). Keep both log line names: renaming one silently disables its
  alarm.
- Retention: inbox rows are deleted 90 days after they were received by the daily `crm_lifecycle` run
  (`crm.lifecycle_purge_expired`, crm-018; count `membership_inbox_purged`) and with the agency at offboarding.

## Content studio invitations (AA-979 CRM-P2b, root ADR 0010a)

CIS owns the Content studio roles; only this API creates partner-pool accounts. So a studio invitation is CIS-created and
CRM-delivered (`app/members/studio.py`, table `crm.studio_invitation`, crm-020, a platform table: the CIS tenant may
have no CRM agency):

- **Issue / send again**: `POST /crm/ops/v1/tenants/{cis tenant}/studio-invitations` `{email, cis_invitation_id,
  locale}` from the CIS staff relay (Tenant 360 Members). One row per CIS invitation id; a new single-use token; the
  address sealed under a key derived from that token (HKDF-SHA256 -> AES-256-GCM: the database alone cannot open it);
  the AA system mail (`studio_invitation`, EN + VI) with `<PARTNER_WEB_ORIGIN>/join?t=<token>`, sent inside the
  transaction. Sending again re-issues the same row while it is pending or expired; claimed / accepted rows are left
  alone (409). Context `studio_platform_tx()` (actor `platform:<staff sub>`).
- **Claim / accept**: the same public claim route and `/crm/v1/invitations/accept` as a CRM invitation; the token is
  tried as a CRM invitation first, then as a studio invitation (`studio_invitation_tx()`, actor `invitation:<id>`).
  Accept binds nothing in the CRM (no `crm.member`): the row becomes `accepted`, a `crm_studio_activation` job is
  queued, one activation is tried at once; the answer is `{kind: "studio", tenant_id: <CIS tenant>, status:
  accepted | activated}`.
- **Activate**: CIS binds the identity (contract `tenant.membership.v1` 1.1); CIS decides the Content studio role.
- No agency audit (no agency may exist); log lines carry ids and codes only, never the address.
- **The Dev-only E2E CIS admin** (root ADR 0006a A10b-1, S252 security review): the CRM is reachable without CIS, so
  it checks again what CIS checks before relaying (`studio.check_e2e_invitation`): a synthetic-only environment
  (`dev` / `test` / `local`), an address on the E2E mailbox domain, a **synthetic CRM agency** with that id
  (`tenancy.synthetic_agency`; a studio-only tenant without a CRM agency is refused, so the phase 2 tenant needs one),
  and an `invited` `viewer` / `editor` membership with that invitation id at CIS (`tenant.membership.v1`
  getTenantMembers, M2M `cis.tenant/read`, `app/cis/invitation_lookup.py`; the body carries no role), in an answer
  where CIS also marks the tenant synthetic (contract 1.2 `is_synthetic` exactly `true`, CIS `shared.tenants.is_canary`;
  the CRM flag alone is weak on Dev, where every agency is synthetic; a 1.1 answer without the field is refused). 403
  `e2e_read_only` otherwise, 503 `cis_unavailable` when CIS cannot answer. Every other caller is unchanged.

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
