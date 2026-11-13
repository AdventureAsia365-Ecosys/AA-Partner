---
id: ARCH-06-partner
title: "AA-Partner — Repo Context"
description: Stack, boundaries, ownership, route classes, tenancy rules and working rules of AA-Partner (partner CRM API, crm.job worker, CRM web, and the tenant Content studio moved from AA-CIS-App); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-crm-api, aa-crm-web]
last_reviewed: 2026-11-05
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
requests the owner confirms, failed-job retry, owner recovery) behind a response key allow-list; and **Module 18 portability and deletion** (AA-967, CRM-35, `crm-026`): the
portability ZIP export (agency or one client) as a `crm.job` with progress and a single-use owner download link, the
data-request log, the verified client erasure receipt, and the agency deletion's cool-off and post-erase verification; and
**onboarding + import** (AA-962, CRM-31, `crm-030`): the getting-started checklist (three required steps, optional ones
with "Later"), Connections and the controller settings, the owner's shared-responsibility acknowledgement, the phased
client import (CSV / .xlsx: clients, relationships, trips, notes) with a dry run and idempotent re-runs as
`crm_client_import` jobs, and the duplicate-client review and merge. Since then, through **crm-035**: AA insights
(`crm-031`), the **AI gateway v2** (AA-964, `crm-032`: the vendored `app/aa_llm/` core of AA-CIS-App, its own control
plane and the `crm.llm_call` ledger in the root ADR 0009 ledger v2 shape; since AA-1027 F4 (`crm-042` and AA-CIS-Infra
`crm_llm.tf`) Dev drafts call Amazon Nova Lite on acc2, the interim path while acc3 refuses every model), the AA staff console with owner-granted support access and audited break-glass (AA-971,
`crm-033`, WORKFORCE `crm-staff` client), ops releases to AA ground operations (AA-956, `crm-034`), commissions and
one-action cancel (AA-957, `crm-035`), and the offline traveller itinerary (AA-954, CRM-24b). The migrations table
below lists every file.

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
  `catalog.http.v1`, `tenant.membership.v1`, `tenant.addons.v1` (AA-CIS-App; AA-1036 R3: the CRM owner's add-on
  request, `app/cis/addons.py`, scope `cis.tenant/addons`). Service calls use the M2M pool (ADR 0006b decision 7):
  `app/cis/m2m.py` asks the aa-m2m pool for one scope per call (AA-978). `tenant.membership.v1` is pulled by
  `crm_membership_pull` (section "CIS membership feed" below); CIS is a revocation and lifecycle source there, never a
  positive gate and never a source of CRM roles.
  AA-947: `booking.b2b.v1` (quotes, holds, confirms, the `/events` feed) through `app/booking/` and `catalog.http.v1`
  (the AA tour cache) through `app/catalog/`, with the same M2M client (section "CRM-19" below). AA-951: `catalog.http.v1`
  1.2 `/aa-content` (AA prep packs and starter templates, AA-977) into `crm.aa_content` (section "CRM-22" below).
  AA-1030: `booking.b2b.v1` is consumed up to **1.4** (`GET /tariff`, the agency's AA tariff; section "CRM-19",
  "AA prices").
- **Publishes** `crm.ops.v1` under `/crm/ops/v1` to the CIS admin (L1 platform console); since 1.5 (AA-966) it also
  carries the AA insights read `GET /insights` (spec module 17, design S-03), instead of a separate `crm.insights.v1`;
  since 1.6 (AA-964) the CRM ledger rows `GET /llm/calls` for the CIS Calls tab (section "CRM-33a" below).
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
| Runtime | ECS Fargate **ARM64** service `aa-crm-dev-api` on `aa-cis-dev-cluster` (acc2, us-west-1) |
| Edge | CloudFront `partner-dev.` -> VPC origin -> internal NLB -> ALB host + `X-AA-Origin` + `/crm/*` rule -> the service (AA-931 spike, ADR 0010 decision 2) |
| Image | ECR `aa-crm-dev-api`, IMMUTABLE, tag = git SHA, built natively on an arm64 runner |
| Web | `web/`: Next.js 16 standalone (React 19, TypeScript, vitest), the AA UI kit core vendored from AA-CIS-App (`web/app/_kit/core/`, root ADR 0016; inline styles from the kit tokens); ECS Fargate **ARM64** service `aa-crm-dev-web` (port 3000, `/healthz`), ECR `aa-crm-dev-web` (AA-CIS-Infra `crm_web.tf`) |
| Portal | retired (AA-1036 R3 slice 2): the tenant Content studio is part of `web/` (`web/app/(studio)`, section "One agency app"); the separate `portal/` app, its Fargate service `aa-crm-dev-portal` and its deploy were deleted after the cut-over. Section "Tenant portal" below is its history |
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
| `/crm/public/v1/p/{token}`, `/crm/public/v1/portal/{token}` | no token (AA-946): the signed link in the path (purpose `proposal` / `portal`), plus in the portal the session cookie `aa_portal_s` a verified e-mail code set (httpOnly, Secure, SameSite=Strict, Path = that portal's own path, 12 h; SHA-256 stored); served on the client host | `client_link_tx()` of the purpose; the proposal page and accept (409 `price_changed` / `terms_changed`), the portal home, codes, session, Stage 3 (403 `stage3_locked` before the deposit), e-sign v1 (AA-955: the envelope, guardian signing, the terms re-accept); writes: Origin check, per-address limit, 32 KiB cap; 404 `link_not_found` for any unusable link. Two per-address buckets (429 `rate_limited` + `Retry-After`): anonymous / pre-sign-in writes (proposal writes, sign-in code, code check) use `RATE_LIMIT_INTAKE_PER_MINUTE` (5); writes only a signed-in traveller can make (Stage 3, flights, passport scan, signing code, sign, companion invite, switch, sign-out) use `RATE_LIMIT_PORTAL_WRITE_PER_MINUTE` (30) when a session cookie is sent, else the intake bucket; a sent cookie also holds a slot of a third bucket (`RATE_LIMIT_INTAKE_PER_MINUTE`, 5) until the session check passes, so forged or stale cookies stay at 5 refused writes per address per minute (AA-953 follow-up; list in `docs/implementation-notes/AA-953-write-limit.md`) |
| `/crm/public/v1/traveller` | no token in the URL (AA-953, CRM-24a; §2.9 rule 1 for the portal): the web landing `/c/t/<token>` exchanges the link for the host-only cookie `aa_portal_link` (httpOnly, Secure, SameSite=Strict, Path `/crm/public/v1/traveller`) and redirects to `/c/t`; the session cookie `aa_portal_s` sits on the same path | `client_link_tx()` purpose `portal` (the cookie's token); the same home / code / session / Stage 3 / sign as the token routes, plus the "Who are you?" picker on `POST /session` (200 `pick` until a `traveler_id` is chosen; Fix E4), `POST /switch` (inside the address's group), `PUT /flights`, `POST /passport-scan` (write-only, 10 MiB body cap, five live scans), `GET /documents/{id}` (403 `document_write_only` / `document_not_shared` / `document_not_yours`), `POST /companions` (lead only); no cookie or a bad one: 404 `link_not_found`; the same two write buckets as the token routes |
| `/crm/public/v1/proposal[/pick\|/hold\|/accept\|/change-request]` | no token in the URL (AA-950, Fix B11): the host-only cookie `aa_proposal_link` the web set when the client opened `/c/p/<token>` (httpOnly, Secure, SameSite=Strict, Path `/crm/public/v1/proposal`); served on the client host | `client_link_tx()` of purpose `proposal` (`app/proposals/public.py`); C-03 / C-04 / C-12 of a builder version (Keep / Swap, Choose later, the hold set, accept 409 `hold_expired`, Ask for a change); writes: Origin check, per-address limit, 32 KiB cap; no cookie or an unusable link: 404 `link_not_found` |
| `/crm/public/v1/r/{token}`, `/crm/public/v1/partner[/code\|/session\|/sign-out]` | no token (AA-1071): a sales partner's signed link: the intake link in the path (purpose `intake`), the view link by the host-only cookie `aa_partner_link` the web set at `/c/a/<token>` (Path `/crm/public/v1/partner`), plus the session cookie `aa_partner_s` an e-mail code set (12 h, SHA-256 stored); served on the client host | `partner_link_tx()` of the purpose (`app/partners/public.py`); the intake form (the public form's checks and answer) and its submission stamped with the partner; the view: code, session, the partner's trips and commissions (no traveller name or contact), sign-out; writes take the intake limiter per address; 404 `link_not_found` for any unusable link |
| `/crm/public/v1/calendar/{token}.ics` | no token header (AA-961): the 43-character feed token in the path only (SHA-256 stored), fetched by the member's calendar app; served on the partner host | `calendar_feed_tx()` (`app/calendar/public.py`); `text/calendar`, no names, titles or money; 404 `feed_not_found` for an unknown, revoked or expired token, an inactive member or a locked agency (subscribed calendars stop updating); public rate limit |
| `/crm/v1` | PARTNER x {AA-Partner web} only (AA-1036: `GET /me` also answers `crm_entitled` for the app switcher and `ai_paused` for the CRM banner; R3: `GET /add-ons` and `POST /add-on-requests`, owner only, relayed to CIS `tenant.addons.v1`) (the Dev EXTERNAL x {`cis-portal`} bridge was removed at AA-979 P2b: its tokens are 401) | `X-AA-Tenant-Id` + `tenant_tx(action=...)` (membership in `crm.member`, then the permission matrix); `GET /crm/v1/memberships` and `POST /crm/v1/invitations/accept` need no header |
| `/crm/ops/v1` | WORKFORCE x {`cis-admin`} | group `cis-admin` or `crm-platform-admin` reads, `crm-platform-admin` writes (AA-939: member deactivation, owner recovery; AA-941: the L1 console, section "L1 console" below). Every response passes the L1 key allow-list (`app/ops/serializer.py`). AA-979: `POST /tenants/{id}/studio-invitations` (the CIS staff relay of a Content studio invitation) takes any OPS caller, `cis-admin` included (section "Content studio invitations"). The Dev-only E2E CIS admin (root ADR 0006a A10b; a `cis-admin` token whose `email` claim is its address, `Caller.e2e_read_only`, `app/auth/e2e_staff.py`) is read-only: every `crm-platform-admin` write answers it 403 `e2e_read_only`. AA-1035 (crm.ops.v1 1.12, root ADR 0019 d4): the CIS desk groups `cis-customer_success` / `cis-support` read only the agencies CIS reports in their scope (`tenant.membership.v1` 1.3 `getStaffTenantScope`, M2M, cached 30 s; an answer about another identity or role than asked is refused; `app/ops/staff_scope.py`): 403 `tenant_outside_scope` outside it, the list filtered before any agency is opened, never a platform-wide read or a write (403 `missing_group`), support never the agency account (`/usage`, `/entitlements`: 403 `missing_group`, as CIS `tenant.book.account.read`), 503 `cis_unavailable` when CIS cannot answer; the studio invitation relay is scoped too, support sends again to the kept address only (crm-045), and a desk caller delivers only an invitation CIS (getTenantMembers of that tenant) reports as an invited `viewer` / `editor` membership (403 `owner_invitation_not_allowed` / `invitation_not_invited`; the owner seat stays with `cis-admin` / `crm-platform-admin`). AA-1035 (root ADR 0006a A10d): the Dev-only CIS customer_success E2E identity (`email` claim `e2e-cis-cs@`, groups exactly `cis-customer_success`, `Caller.e2e_no_write`, never `e2e_read_only`, which would open the A10b-1 invitation path) is refused outside dev / test / local (403 `e2e_read_only`), its desk reads are scoped by CIS like any customer_success member, and it writes nothing: `require_group` and the studio invitation relay refuse it with 403 `e2e_read_only` before any check |
| `/crm/staff/v1` | WORKFORCE x {`crm-staff`} | group `crm-ops` or `crm-support`; AA-971 (CRM-13b): support needs the owner's live grant, break-glass group `crm-ops`; every agency read through `support_tx()` as `crm_support_reader` (section "AA support access" below). AA-1019: the Dev-only CRM staff E2E identity (root ADR 0006a A10c, `email` claim `e2e-crm-staff@`, `Caller.e2e_synthetic_only`) is refused outside dev / test / local (403 `e2e_read_only`) and reaches synthetic agencies only, never the pilot agency (`crm.tenant.is_pilot`, AA-1086 G9) (`ops_tx` / `staff_tx` / `support_tx`: 403 `e2e_synthetic_only`; the lists skip others). The web console is section "AA staff console" below |

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

- **Fair share and body ceiling** (AA-969, CRM-37a security review; `docs/operations/slo.md`): every request-path
  transaction first takes a slot of `app/tenant_gate.py`: at most 2 of the pool's 3 connections for the whole request
  path (the third stays for the crm.job worker), at most `CRM_TENANT_MAX_CONCURRENCY` (default 2) per key and never
  every request slot to one key (ordinary holds capped at total - 1 = 1), handed out fairly (the waiting key holding
  the fewest slots first, so a calm agency gets the next free slot ahead of the busy agencies' queues). Work that
  holds its slot across a slow external call asks for a **long** slot (`tenant_tx(..., long=True)`: the AI draft, the
  document upload and download); at most total - 1 = 1 long slot is held at once, so one slot always serves short
  transactions. Keys: `tenant_tx()` the agency once the caller's membership was verified in this task, else the
  caller; `caller_memberships()` the caller; the anonymous contexts (intake, invitations, client links, calendar feed)
  one shared public key; Twilio webhooks `class:hook` for a hook id this task already resolved, else
  `class:hook-unknown`. 429 `tenant_busy` (503 `db_busy` for the anonymous keys) after `CRM_TENANT_SLOT_WAIT_MS` (4x for
  a long slot), then at most 10 s for a connection (503 `db_busy`). The agency class guard adds a
  per-member-per-agency ceiling (`RATE_LIMIT_MEMBER_PER_MINUTE`, 429 `rate_limited`) keyed by the canonical agency
  uuid only once the membership was verified in this task, else one `unverified` bucket per caller. An ASGI
  middleware (`app/bodylimit.py`) caps every request body before any route code runs (FastAPI parses JSON before the
  class guard): 1 MiB, the four upload routes their own cap + 64 KiB; 413 `body_too_large`. A body past 64 KiB on a
  class route is read only once its bearer token verifies (else 401 before more is buffered) and is charged to the
  verified budget (32 MiB, one identity at most one mail import); anonymous routes have their own 12 MiB budget, one
  passport scan per client address (503 `server_busy` when full); and a body past 64 KiB must keep 16 KiB/s after 20 s
  of grace, else 408 `body_too_slow`. `POST /crm/v1/documents` reads its JSON body after the class guard.
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
- **`calendar_feed_tx(pool, token_hash_hex)`** (AA-961) is a member's ICS feed fetched by a calendar app: the token's
  SHA-256 sets `app.calendar_feed_hash`, read by the crm-027 SELECT-only policy `calendar_feed_token_lookup` (TO
  `crm_app`, active and unexpired feeds only); the feed decides the agency (must be active) and the member (must be
  active), `app.actor` = `calendar_feed:<id>` (audit actor kind `user`; the crm-027 guard lets it touch only that
  feed's fetch counters). Any unusable token is 404 `feed_not_found`. Only `app/calendar/public.py` may open it.
- **`payment_session_tx(pool, txn_ref)`** (AA-1072) is a payment gateway's webhook (the VNPay IPN, the Dev stub
  gateway): the merchant transaction reference the gateway names sets `app.payment_txn_ref`, read by the crm-057
  SELECT-only policy `payment_session_ref_lookup` (TO `crm_app`, live sessions only); the session decides the agency
  (must be active), `app.actor` = `job:crm_payment_hook`. Any unusable reference is 404 `session_not_found` (the route
  answers VNPay's `01`). The gateway signature is checked with the agency's own secret before anything is written.
  Only `app/payments/gateway.py` may open it (`test_code_rules.py`).
- **`ingest_tx(pool, local_part)`** (AA-945) is the forward-in mail's context: the recipient's local part (an
  unauthenticated sender chose it) sets `app.ingest_local_part`, read by the crm-015 SELECT-only policy
  `ingest_address_lookup` (TO `crm_app`, active and live addresses only); the address decides the agency (must be
  active), `app.actor` = `job:crm_mail_ingest`. Unknown, inactive or a locked agency = 404 `address_not_found` (the mail
  is discarded, card R). Only `app/mail/ingest.py` may open it.
- **`staff_tx(pool, tenant_id, actor="support:<sub>" | "breakglass:<sub>", statuses=...)`** (AA-971) is AA staff
  opening, ending or looking up their **own** support / break-glass session in one agency (audit kinds `support` /
  `breakglass`, login role `crm_app`, the session rows only; crm-033 `crm.guard_support_access` binds each row to its
  actor). Only `app/support/sessions.py` may open it.
- **`support_tx(pool, caller, tenant_id)`** (AA-971) is AA staff **reading** one agency: the caller's newest session
  there (by WORKFORCE `(iss, sub)`) must be active, unexpired and, for support, inside the owner's live grant, on every
  request; then `app.actor` = `support:<sub>` / `breakglass:<sub>` and `SET LOCAL ROLE crm_support_reader` (named
  columns of four non-F1 tables, no write privilege). Refusals are 403 (`no_support_session`,
  `support_session_ended`, `support_access_expired`, `tenant_locked`) with a committed `denied` event.
  **`as_login_role(ctx)`** steps out of the reader for one block (the party's display names, only when the session
  shows identity). Only `app/support/reads.py` opens / calls them (`test_code_rules.py`).
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
  sender `app/mail/sender.py` (AA-945; the mailbox health check opens credentials through it), the text-message sender
  `app/messaging/sender.py` (AA-958) and the agency's own payment gateway `app/payments/gateway.py` (AA-1072: the pay
  URL signer, the IPN check and the Dev stub); no route
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
| `crm-025` | `api/migrations/025_crm_contracts_waivers.sql` | CRM-25 Module 12 e-sign v1 (AA-955; needs crm-024; contract class under ADR 0017: a widened CHECK, a replaced key and sealed trigger, replaced `crm.erase_subject` (the crm-024 text) and `crm.lifecycle_purge_expired` (the crm-018 text)): `agreement_doc.kind` + `insurance_declination` (R13); `signature_request` + the envelope (`signer_traveler_id` / `signer_role` self or guardian, generated `access_traveler_id` naming the signing access, terms and declination versions with their SHA-256, `insurance_choice`, `envelope_sha256`, `seal_alg`; sealed once signed); `terms_acceptance` and `signature_event` (append-only, `crm.ledger_append_only`; no foreign key on trip / proposal / traveller / request: the DM-7 claim-period exception keeps them through a trip or traveller erasure until `keep_until` = trip end + 3 years, then `crm.lifecycle_purge_expired` deletes them, never a held row; the agency erase takes them; the receipt counts `*_kept`) |
| `crm-026` | `api/migrations/026_crm_portability_requests.sql` | CRM-35 Module 18 (AA-967; needs crm-025, contract class): `crm.export_job` (base template; one portability export of the agency or of one client: queued -> building (progress) -> ready -> expired / cancelled, or failed; object `exports/tenants/<tenant>/portability/<export>.zip.enc`, its and the ZIP's SHA-256, size, row counts, the single-use download link's SHA-256 + expiry; one open export per subject) and `crm.data_request` (base template; the agency's data-request log: kind, channel, client id without foreign key, received / due (<= +92 days), open -> extended -> done / refused / withdrawn + outcome, the erasure receipt); both crm_app SELECT / INSERT / UPDATE, no reader grant, and both on the agency erase keep list (receipts without personal data); `crm.erasure_request.erase_not_before` (the deletion cool-off) and `verified_at`, `UNIQUE (tenant_id, request_id)`; `crm.erase_subject` = the crm-025 text + the two keep-list names. No new SQLSTATE |
| `crm-027` | `api/migrations/027_crm_duty_of_care_calendar.sql` | CRM-30 duty of care + calendar (AA-961; needs crm-026; contract class under ADR 0017: a replaced `crm.erase_subject`, the crm-026 text with two agency-scope tables): `crm.incident_flag` (base template: an AA incident on a departure as the Booking feed reported it, departure id, the contract's incident code, open -> cleared, raised / cleared times and Booking event ids; one open flag per departure; ids and codes only; written by job / platform actors only, `crm.guard_incident_flag` CRM41), `crm.calendar_feed` (base template: one ICS subscription per member, the token's SHA-256 only, scope `mine` / `agency`, active -> revoked, expiry <= 400 days, fetch counters; one active per member; SELECT-only `calendar_feed_token_lookup` on `app.calendar_feed_hash`; `crm.guard_calendar_feed` CRM42: own feed only, token / member / scope / expiry fixed, only the member or an owner revokes, the fetch actor touches only its counters); crm_app SELECT / INSERT / UPDATE, no reader grant |
| `crm-028` | `api/migrations/028_crm_invoice_claims.sql` | AA-952 CRM-23 slice 4 (needs crm-027; **expand** class under ADR 0017: ADD COLUMN only, shipped in the code PR and auto-applied before the deploy): `crm.invoice` + `claimed_at` (the client's "I have paid"), `hold_extended_at` / `hold_extend_code` (Booking's one hold extension), `rehold_requested_at` / `rehold_quote_id` / `rehold_price_hash` / `rehold_code` (the re-hold of a late deposit), `last_reminded_at` / `reminder_count` (overdue reminders); the value lists are the API's (no CHECK in an expand file) |
| `crm-029` | `api/migrations/029_crm_traveler_flight.sql` | CRM-24a portal per person (AA-953; an **expand** file, ADR 0017: one new table and one new nullable column, so it ships in the code PR): `crm.otp_challenge.sent_to_bidx` (the blind index of the address a portal code went to: a session's proof) and `crm.traveler_flight` (base template; one arrival / departure flight per trip, traveller and direction: `flight_no_enc` under the vault DEK, `airport_iata`, `sched_local` (local time at that airport, no zone) + `tz` (IANA), `entered_by_kind`; all `pii:travel`; RLS, hold guard, no reader grant). Every key cascades (trip, traveler, trip_traveler), so the erase deletes flights through the rows it already deletes and `crm.erase_subject` is not replaced (`test_crm010_schema.py` `CASCADE_ERASED` checks the keys). needs crm-028 (AA-952) |
| `crm-030` | `api/migrations/030_crm_onboarding_import.sql` | CRM-31 onboarding + import (AA-962; expand class under ADR 0017: nullable columns, columns with a constant default, non-unique partial indexes, column comments; no `crm.erase_subject` change: the new columns go with their rows): `import_ref_bidx` on `crm.traveler` / `crm.trip` / `crm.note` (blind index of the agency's own ref of an imported row; a partial non-unique index each, like every blind index here), `crm.traveler.import_ref_alias_bidx` (`bytea[]`, default empty: the other refs a client answers to after a merge or a match; a non-unique GIN index), `crm.tenant_settings` `onboarding_later` / `onboarding_done` (step codes), `privacy_notice_url`, `data_request_contact`, `consent_text` (controller settings), `responsibility_ack_version` / `_at` / `_member_id` (the owner acknowledgement). Values are checked by `app/onboarding.py` (an expand file adds no CHECK to an existing table) |
| `crm-031` | `api/migrations/031_crm_insight_trip_fact.sql` | CRM-34 AA insights (AA-966; expand class under ADR 0017, applied after crm-028..030 in number order): the view `crm.v_insight_trip_fact` (security_barrier; trips per agency, month, budget band, lead age band, party size, stage and Lost reason in `trip_count`, no destination, no trip id; GROUP BY, so not updatable), SELECT to `crm_insights`, so module 17 counts a trip's party once outside the destination dimension. No existing object changes |
| `crm-032` | `api/migrations/032_crm_llm_call_v2.sql` | CRM-33a AI gateway (AA-964; contract class under ADR 0017: new CHECKs and a replaced role CHECK on the ledger, so MIG-4 order, manual DB Migrate apply before the code PR): `crm.llm_call` follows the root ADR 0009 ledger v2 contract (`app` NOT NULL DEFAULT `crm`, CHECK = `crm`; `attempt_no`, `request_id`, `job_id`, `connection_key`, `deployment_wire_id`, `family`, `cache_read_tokens` / `cache_write_tokens`, `units` / `unit_kind`, `pricing_mode`, `prompt_hash`, `prompt_version`, `is_shadow` / `is_experiment` NOT NULL DEFAULT false), role CHECK + `guard` (the Bedrock Guardrails row, stage `crm_scrub`); append-only trigger, FORCE RLS and grants unchanged. `crm.tenant_settings.ai_alert_month` / `ai_alert_pct` (75 / 90 / 100, both or neither): the month's highest AI budget alert (R5) |
| `crm-033` | `api/migrations/033_crm_support_access.sql` | CRM-13b AA support access (AA-971; contract class under ADR 0017, applied before crm-034): `crm.support_grant` (the owner's grant: one active per agency, a 1 to 7 day window, `show_identity`; active -> revoked \| expired) and `crm.support_session` (one AA staff member in one agency: kind `support` inside the grant, or `breakglass` with a reason code, at most one hour, identity never shown; the ticket and the WORKFORCE `(iss, sub)` of the staff member; `owner_notified_at` for the break-glass mail), guard `crm.guard_support_access` (binds each row to its session), and the NOLOGIN reader `crm_support_reader` with SELECT on named columns of four non-F1 tables; every read lands in the agency's own audit |
| `crm-034` | `api/migrations/034_crm_ops_release.sql` | CRM-26 Module 8 ops release (AA-956; **expand** class under ADR 0017, applied after crm-033): `crm.ops_release` (base template; one live release per trip, partial unique on `purged_at IS NULL`; `payload_enc` under the agency's `ops_release` DEK, `payload_version`, `fields_released`, `completeness` counts, `complete`, Booking `departure_ids` / `bk_booking_ids`, dates, `purge_at` = end + 90 days, `t21_alerted_at`, withdraw + purge with a `deletion_receipt`, view counters; cascades from its trip) and `crm.supplier_manifest_export` (one minimal manifest AA ops exported: version, field codes, csv, row count, watermark; no personal data; cascades from its release); guards `crm.guard_ops_release` / `crm.guard_supplier_manifest_export` (CRM45: a job creates and purges, `ops:<sub>` only counts a view or inserts a manifest record, a member only withdraws, nobody deletes, a held trip's purge CRM10); no reader grant |
| `crm-035` | `api/migrations/035_crm_commission_cancel.sql` | CRM-27 Module 13 commission + one-action cancel (AA-957; **contract** class under ADR 0017: widened CHECKs on `crm.aa_request.kind` (+ `cancel`) and `crm.booking_line_money.kind` (+ `cancellation_cost`), a new column `crm.agency_supplier.commission_due_days`, `crm.status_transition` rows, so MIG-4 order, manual DB Migrate apply before the code PR): `crm.commission` (base template; one live row per confirmed agency line with an expected commission: expected -> claimed -> received | written_off, or cancelled; due date; the receipt frozen at the receipt day's rate, `short_amount` in the original currency only, `fx_difference_base`; RESTRICTIVE `commission_margin_roles`; crm_app SELECT + UPDATE of the follow-up columns, guard CRM46), `crm.trip_cancellation` (base template; one per trip, pending until Booking cancelled the trip's AA bookings, then done; crm_app SELECT only, written by `crm.trip_cancellation_open` (owner / agent; the database decides pending / done from the live AA bookings) and `crm.trip_cancellation_finish` (done only once no AA booking is live and the trip is cancelled)), `crm.commission_sync(trip)`, `crm.commission_receive(...)`, `crm.booking_line_aa_cancel_fee(...)` (SECURITY DEFINER); both tables cascade from their trip (and line), no `crm.erase_subject` change |
| `crm-036` | `api/migrations/036_crm_messaging.sql` | CRM-28 Module 9 SMS + WhatsApp (AA-958; **contract** file under ADR 0017 (no expand directive; applied by hand with DB Migrate)): `crm.messaging_connection` (base template; the agency's one live Twilio / sandbox connection, credential key ON DELETE CASCADE from `crm.integration_credential` provider `twilio`, `hook_id` = 32 random hex characters of the webhook path, the SMS and WhatsApp senders, last inbound / status times; SELECT-only lookup policy `messaging_connection_hook_lookup` on `app.messaging_hook_id`, set by `app.tenancy.messaging_hook_tx()`), `crm.channel_template` (base template; the WhatsApp templates of the agency's account: `content_sid`, name, language, category, approval, body with `{{n}}`, variables; cascades from its connection), `crm.message_delivery` (base template; one per outbound SMS / WhatsApp `crm.message`: template + sealed variables, `provider_status`, `status_rank`, `error_code`, `status_at`; crm_app no DELETE), `crm.conversation.wa_window_expires_at` + `opted_out_at`; every new table cascades from a row the erasure already deletes, no `crm.erase_subject` change |
| `crm-037` | `api/migrations/037_crm_llm_stage_profile.sql` | AI gateway on the shared core (AA-964, S263 gateway plan F1 + F2; **contract** class under ADR 0017: a replaced outcome CHECK on the `crm.llm_call` ledger, so MIG-4 order, manual DB Migrate apply before the code PR): `crm.llm_stage_profile` (platform configuration, no tenant column: one row per CRM stage, role, `is_active` = the stage kill switch, `max_attempts` 1..6, `route_deadline_s` 1..120, `degrade_mode` `manual_reply_needed`) and `crm.llm_stage_route` (the stage's Bedrock route in order, position 0..5: model wire id, family = maker, catalog prices per million tokens both or neither, `is_enabled`); crm_app SELECT only, no reader grant; seed `crm_draft_reply` (writer, 2 attempts, 30 s: Haiku 4.5, then Sonnet 4.5). `crm.llm_call.outcome` + `billed_unusable`, `truncated` (ledger v2 statuses). crm-036 is AA-958's (CRM-28 messaging), which merges first |
| `crm-038` | `api/migrations/038_crm_workflows_tasks.sql` | CRM-29a Module 10 workflows + tasks (AA-959; **contract** class under ADR 0017: new columns, a CHECK and a re-created hold guard on `crm.task`, a new `crm.tenant_settings` column, `crm.status_transition` rows, a replaced `crm.erase_subject`, so MIG-4 order, manual DB Migrate apply before the code PR): `crm.workflow_rule` (base template; the agency's automations: trigger `stage_enter` / `event` / `date_anchor` / `special_date` / `manual` (a task list), offset days, conditions and 1..20 steps as JSON validated by the API, active / paused / archived, `version`, `template_key` of the AA library template, one live rule per template; crm_app no DELETE) and `crm.workflow_run` (base template; one run of a rule for one trip or one client, `UNIQUE (tenant, rule, trigger_key)` = the idempotent tick, active -> completed / stopped / failed, `step_index`, `next_at`, `stop_reason`, a log of codes and ids; hold guard; crm_app no DELETE); `crm.task.traveler_id` (a client task: a trip or a client, never both), `title_enc` (vault DEK: a custom task, kind family `custom_<hex>`), `workflow_run_id`; `crm.tenant_settings.task_digest`; `crm.erase_subject` = the crm-033 text + the workflow tables (trip, traveller and agency scopes) |
| `crm-039` | `api/migrations/039_crm_partner_account_cleanup.sql` | S263 AA-939 follow-up (**contract**: a changed policy): `studio_invitation_platform` (crm-020) also lets `job:crm_partner_accounts` read `crm.studio_invitation` (USING only; its WITH CHECK and guard CRM37 refuse that actor's writes), so the partner-account clean-up sees every studio link |
| `crm-040` | `api/migrations/040_crm_ai_draft.sql` | AA-1027 F3 (**expand**: one new table + guard): `crm.ai_draft` (one row per answered AI draft of the trip workspace: kind, ledger call id, prompt version, the draft with its placeholders under the vault DEK, one `decision` pending -> accepted / discarded with an optional R39 `feedback_code`), guard `crm.guard_ai_draft` (CRM47; `decision`, not `status`, so no `status_transition` rows); cascades from its trip, no `erase_subject` change |
| `crm-041` | `api/migrations/041_crm_llm_app_profile.sql` | AA-1025 H1c (**contract**: REVOKE of the default privileges, so MIG-4): `crm.llm_app_profile` (platform configuration, at most one row `app = 'crm'`: `ai_enabled`, `bulk_paused`, `default_daily_budget_usd` 0..100000, `degrade_mode` `manual_reply_needed`, `disabled_reason` <= 500, `updated_at` / `_by`), no seed row (no row = AI on, no daily budget); crm_app SELECT, INSERT, UPDATE (no DELETE: a deleted row would switch AI back on), no reader grant |
| `crm-042` | `api/migrations/042_crm_draft_route_acc2_nova_lite.sql` | AA-1027 F4 interim path (**contract**: an UPDATE and an INSERT of configuration rows in `crm.llm_stage_route`, so MIG-4; no code depends on it): `crm_draft_reply` routes to Amazon Nova Lite on acc2 (position 2, `us.amazon.nova-lite-v1:0`, family `amazon`, 0.0770 / 0.3080 USD per million tokens = the CIS catalogue row `nova-lite`); the crm-037 Claude Haiku / Sonnet entries are kept but `is_enabled = false` (acc3 refuses every model, acc2 cannot run Claude); the acc3 return is a forward file that switches them back on |
| `crm-044` | `api/migrations/044_crm_tenant_pilot_marker.sql` | AA-969 part 2 (root ADR 0019 d16; **contract**: ALTER of `crm.tenant` + triggers, MIG-4): `crm.tenant.is_pilot` (boolean, default false), the CIS pilot marker projected by `crm_entitlement_pull`; `tenant_one_pilot_uq` (at most one pilot); `crm.guard_pilot_marker()` + triggers `tenant_pilot_marker_ins` / `_upd`: only the account feed actor `platform:feed-tenant-account` (or an unscoped owner session) sets or changes it, anyone else CRM04 `platform_only` |
| `crm-045` | `api/migrations/045_crm_studio_invitation_kept_address.sql` | AA-1035 R2 close-out (**contract**, MIG-4; crm.ops.v1 1.12): `crm.studio_invitation` also keeps the invited address sealed under a one-off data key of the CRM KMS key (`email_kept`, `email_kept_dek`, `email_kept_kek_provider`, `email_kept_kek_id`, all or none), so a send again with `keep_address` goes to the same address; the guard (CRM37) lets only a platform actor set it, the token holder may only clear it (the claim does), a revoke clears it, the job never touches it; an expiry keeps it (an expired invitation is re-issued) |
| `crm-046` | `api/migrations/046_crm_retail_markup.sql` | AA-1030 (**contract**: ALTER + CHECK on `crm.tenant_settings`, so MIG-4): `tenant_settings.retail_markup_pct` (JSONB, default `{}`: the owner's default retail markup in percent per AA product type, `trip_plan` / `hotel_room` / `transfer` / `guide` / `activity` / `entrance` / `meal`, 0..300 with 2 decimals), shape rule `crm.retail_markup_ok(jsonb)` (EXECUTE to crm_app, the writer) and `tenant_settings_retail_markup_chk`; no reader grant |
| `crm-048` | `api/migrations/048_crm_retail_pricing.sql` | AA-1066 retail pricing (**contract**: ALTERs of `crm.tenant_settings`, `crm.proposal`, `crm.option`, `crm.status_transition` rows, so MIG-4): `tenant_settings.child_age_max` / `infant_age_max` (default 11 / 1) and `discount_owner_limit_pct` (default 10), `proposal.discount_pct` (builder only, 0 < pct <= 100; sealed after draft: `proposal_sealed` recreated with the crm-019 list plus `discount_pct`, CRM31), `option.price_rules` (the rules a sell was suggested with, a JSON array snapshot); `crm.option_price` (base template; one row per traveller type `adult` / `child` / `infant` / `single_supp`: unit sell, unit net, qty; draft-only through `crm.guard_proposal_draft`; cascades from its option), `crm.retail_price_rule` (base template; season / group_size / early_booking / last_minute with exactly its own condition, AA product scope, adjust_pct -90..300 not 0, priority, stacking `add` / `best_only`; cascades from the agency's `tenant_settings` row), `crm.discount_approval` (base template; pending -> approved / rejected / superseded, one pending per version, guard `crm.guard_discount_approval` CRM48: only an owner decides, as themselves; no crm_app DELETE; cascades from its proposal). `crm.erase_subject` unchanged (the three tables are cascade-erased, `test_crm010_schema.py`) |
| `crm-049` | `api/migrations/049_crm_cancellation_policy.sql` | AA-1067 (**expand**: three new tables + guard): `crm.cancellation_policy` (versions of the agency's traveller cancellation policy: name, version, `replaces_policy_id`, currency of minimum charges, `is_default` (one live default), `published_at` set once, `retired_at`), `crm.cancellation_policy_tier` (`days_before_min`, `charge_pct`, `min_charge_amount`; one per day count; only while its policy is a draft), `crm.cancellation_charge` (what one trip cancellation charged: policy, days, tier percent, policy charge, final charge with decision policy / override / waived / no_policy and reason, paid, kept, refund due, fee invoice; one per trip, insert-only); guard `crm.guard_cancellation_policy` (CRM49); cascades from `tenant_settings` / the policy / the trip, no `erase_subject` change |
| `crm-050` | `api/migrations/050_crm_payment_schedule_instalments.sql` | AA-1067 (**contract**: ALTERs, seals recreated, an index replaced, so MIG-4): `payment_schedule.kind` + `instalment`, `seq` (1..10, 1 for deposit / balance), `percent`; live unique index on (tenant, proposal, kind, seq); the seal covers both; `proposal.schedule_plan` (builder only, 1..12 lines) and `proposal.cancellation_policy_id` (no key), both sealed after draft; `tenant_settings.schedule_template`; `invoice.kind` + `instalment` (with its row) and `cancellation_fee`; view `crm.v_schedule_status` (security_invoker: paid / partial / overdue derived, `crm_app` SELECT); index `payment (tenant_id, schedule_id)` |
| `crm-052` | `api/migrations/052_crm_agency_service_catalogue.sql` | AA-1069 (**expand**: two new tables + guard): `crm.agency_service` (the agency's own catalogue of root ADR 0019 b6 ancillaries: own supplier or none, category flight / insurance / visa / transfer / fee, name, `unit_net` (owner / agent), `unit_sell`, currency, `qty_basis` per_person / per_adult / per_booking / per_room / per_night / per_person_night, validity window, `active`; cascades from `tenant_settings` and its supplier) and `crm.agency_service_tier` (group-size `unit_net`, ranges never overlap); guard `crm.guard_agency_service` (CRM52: owner / agent or a job / seed actor writes; a live own supplier or none) |
| `crm-053` | `api/migrations/053_crm_supplier_requests.sql` | AA-1069 (**contract**, MIG-4): `crm.booking_line` status + `requested` (planning -> requested -> confirmed / planning / cancelled), `requested_at`, `request_ref` (the request e-mail's message id; agency lines only, `booking_line_request_chk`); `crm.booking_line_confirm` also confirms a requested line; `crm.conversation.agency_supplier_id` (the trip's supplier request thread: a trip, no client, e-mail; one live per trip and supplier) |
| `crm-054` | `api/migrations/054_crm_travel_ops.sql` | AA-1070 (**expand**: six new tables + two guards): `crm.aa_departure_state` (the Booking feed's state of one AA departure: scheduled / guaranteed / cancelled, times, min to operate, last event; job writes only, CRM53; cascades from `booking_feed_state`), `crm.booking_line_voucher` (sealed pick-up, drop-off, PNR, note + start time of a line), `crm.room_assignment` (the rooming list), `crm.visa_template` (the agency's visa guidance), `crm.visa_application` (sealed visa number and validity), `crm.visa_checklist_item`; every key cascades; hold guard `crm.guard_held_travel_doc` (CRM10) on the trip-scoped tables |
| `crm-055` | `api/migrations/055_crm_travel_ops_settings.sql` | AA-1070 (**contract**, MIG-4): `tenant_settings.passport_valid_months` (default 6, 0..24), `crm.document` kinds `confirmation_pdf` / `proposal_pdf` and uploader `system`, the `crm.visa_application` status moves in `crm.status_transition` |
| `crm-056` | `api/migrations/056_crm_sales_partners.sql` | AA-1071 (**contract**: ALTERs of `crm.trip`, `crm.form_submission`, `crm.conversation`, `crm.dispatch_queue`, a trigger on `crm.trip`, `crm.status_transition` rows, a replaced view, so MIG-4): `crm.sales_partner` (kind, sealed name and contact, commission terms percent / fixed_per_pax / net_rate, basis revenue / margin, payout days, active <-> inactive; cascades from `tenant_settings`), `crm.trip.sales_partner_id` (key on (tenant, partner)), `crm.form_submission.sales_partner_id`, `crm.partner_commission` (one live per trip and partner: accrued -> approved -> paid, approved -> accrued, accrued / approved -> cancelled; amount + currency, base amount frozen at the computing day; RESTRICTIVE margin policy; cascades from trip and partner), `crm.partner_commission_sync(trip)` (SECURITY DEFINER) + trigger `trip_partner_commission_sync`, `crm.sales_partner_link` (intake / view, token SHA-256, <= 400 days, lookup policy on `app.partner_link_hash`), `crm.sales_partner_otp` (view codes and sessions, 24 h), `conversation.sales_partner_id` + `dispatch_queue.sales_partner_id` (traveler_id nullable, exactly one subject; gate code `partner_inactive`), `crm.v_trip_pnl` partner commission; guard `crm.guard_sales_partner` (CRM54); no `erase_subject` change |
| `crm-057` | `api/migrations/057_crm_public_booking_online_payment.sql` | AA-1072 (**contract**, MIG-4; crm-056 is another card's, independent): `tenant_settings.public_booking_enabled` (default false: "Book now" off until the owner turns it on); `crm.public_booking_attempt` (base template; one "Book now" press of the public tour page: intake form, AA tour and departure, party counts and ages, quote and total, received -> created / refused / failed with a code, the trip and proposal ids, one per client submission id, `ip_hash` = HMAC of the address under the agency's index key; cascades from `tenant_settings`; deleted after 90 days by the date rule); `crm.payment_session` (base template; one hosted payment of an invoice on the agency's own gateway: account, `txn_ref` unique across agencies, amount, currency, expiry, open -> paid / failed / review, the gateway's transaction number and response code, the payment it wrote; SELECT-only `payment_session_ref_lookup` on `app.payment_txn_ref`; cascades from its invoice); `integration_credential.provider` + `vn_gateway`; `payment.method` + `online`, `payment_provider_ref_uq` (one payment per gateway reference and agency) |
| `crm-058` | `api/migrations/058_crm_hold_guard_tenant.sql` | AA-1085 (**contract**, MIG-4: replaces six trigger functions, adds a trigger): `crm.hold_trip_of` / `hold_traveler_of` / `hold_row_of` (the crm-010 hold rule 4a-4d with an explicit `tenant_id = p_tenant` on every read); the hold guards `guard_held_change`, `guard_message`, `guard_held_flight`, `guard_ops_release`, `guard_supplier_manifest_export`, `guard_held_travel_doc` read the holds of `OLD.tenant_id` only, so a foreign-key cascade (owner, no row security) never sees another agency's hold; the flight and travel document guards check cascades too; `crm.partner_commission` gets `guard_held_change('trip_id')`. Lint: `tests/unit/test_aa1085_hold_guard_tenant.py` |
| `crm-059` | `api/migrations/059_crm_proposal_inclusions.sql` | AA-1070 item crm-35 (**expand**: two new tables): `crm.inclusion_item` (the agency library of standard proposal lines: kind `included` / `not_included`, `text_en` (default) + optional `text_vi`, 1..500 characters, `sort_order` 0..999, `archived`; one live line per kind and English text, case-insensitive; cascades from `tenant_settings`), `crm.proposal_inclusion` (the lines of one builder version: source `library` (text copied, `inclusion_item_id` a snapshot reference without a key) / `custom` / `aa_tour` (an AA tour's inclusion or exclusion copied at Publish: `aa_tour_source_id`, `aa_tour_title`), `sort_order` 0..199, `source_inclusion_id` of a working copy; draft-only through `crm.guard_proposal_draft` (CRM35), trip hold guard; cascades from its proposal); no `erase_subject` change |
| `crm-060` | `api/migrations/060_crm_aa_cancel_case.sql` | AA-1070 item crm-13 (**expand**: one new table): `crm.aa_cancel_case` (one agency trip with a live AA line or proposal option on a departure AA cancelled: opened once per trip and departure by the Booking feed job; `resolution` NULL (open) -> `requoted` (`proposal_id`, `rebook_trip_id`) / `cancelled`; no status column, the API only moves it forward); unique per (trip, departure); cascades from the trip; hold guard `crm.guard_held_travel_doc` (CRM10). (crm-059 is AA-1070 item crm-35, another lane.) |
| `crm-061` | `api/migrations/061_crm_tags.sql` | CRM-29b tags (AA-960; **expand**: two new tables + two guards): `crm.tag` (agency / system, trip / client, fixed code, name, tone, archived; one per agency, subject and code; never trashed; cascades from `tenant_settings`), `crm.tag_link` (one tag on one trip or client, source member / system + reasons; one per tag and subject; cascades from tag, trip, traveller; hold rule 4h), `crm.guard_tag()` / `crm.guard_tag_link()` (CRM55: system tags by job / platform / seed actors only, kind / subject / code fixed, a link of the tag's subject and source) |
| `crm-062` | `api/migrations/062_crm_book_now_price_ceiling.sql` | AA-1092 residual (**contract**, MIG-4; crm-062..065 are other cards' of the same wave, independent): `tenant_settings.public_price_per_minute` (SMALLINT, default 60, CHECK 10..600): the "Book now" price checks a minute the agency's public page takes from every address together (per API task), the owner's setting on Settings > Payments "Online booking"; past it the price check answers 429 `agency_rate_limited`. |
| `crm-063` | `api/migrations/063_crm_tour_retired_flag.sql` | AA-1093 part 1 (**expand**: one new table): `crm.tour_retired_flag` (one item of an agency trip on an AA tour Adventure Asia no longer serves: `kind` open_option / held_line / quoted_line / confirmed_line, `option_id` or `booking_line_id` as snapshot references, `state` open -> cleared (a `state` column, not `status`: no `crm.status_transition` rows, so the file stays expand), `raised_key` / `cleared_key` = the transition); one flag set per tour, transition and item, one open flag per item; cascades from the trip; hold guard `crm.guard_held_travel_doc` (CRM10); no reader grant. |

Flow (data standards D8 / MIG-4): the file merges here first, then a byte copy goes to AA-CIS-Infra
`db/migrations/crm/` (Migration Copy Guard compares it with this repo's `main`), then DB Migrate `check` -> `apply`
runs it as `crm_owner`, then the `bot/db-schema-reference` PR is merged, then code that reads the new objects deploys.
`/crm/health/ready` stays 503 until every key in `app/db.py` `REQUIRED_SCHEMA_VERSIONS` is registered. Its body lists `registered_keys` (every registered `crm-NNN`, sorted) whenever the schema read succeeds (AA-993 M4a,
root ADR 0017 d7: the Deploy Dev `schema-gate` job waits on it before the roll). CI job **Migration preflight**
(`ci.yml`) checks a new file's number (`main` max + 1) and class (expand: `-- migrate: expand`, auto-applied on Dev;
contract: manual DB Migrate apply) and refuses an added line that names a migration by an old number after a renumber
(`.github/scripts/migration_refs.py`, AA-1024 l22; mark a history note `old-mig-ok`), and `migration-sync-wake.yml` rings the AA-CIS-Infra Migration Sync doorbell after a
push to `main` that touches `api/migrations/`, so the byte copy opens at once.

## Job runner (`crm.job`, ADR 0003a)

Ported from AA-Booking (AA-751 rules): `FOR UPDATE SKIP LOCKED` claims (AA-969: **fair per agency**, not oldest
first: the due row whose agency (`payload->>'tenant_id'`) was served least in the last 5 minutes goes first, oldest
first inside an agency, a row without an agency is its own group; `app/jobs/queue.py` `CLAIM_SQL`), guarded state
changes, lease + heartbeat,
reaper (a stopped worker's job is re-claimed), release on graceful shutdown, `cancel_requested`, backoff, the backlog
sampler (S263 alert diet, AA-1025: no CloudWatch metric; it runs the in-app ops checks of
`app/observability/ops_alerts.py`, see "Ops checks" below). CRM differences: the job row keeps the **error class only** (crm-001 column
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
| `crm_lifecycle` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook (`DailyLifecycleSchedule`) | AA-942: per agency (active / locked): form-submission payloads redacted after 90 days, LLM calls older than 180 days purged (with their crm-009 placeholder maps), CIS membership inbox rows older than 90 days purged (crm-018), trash past its window purged through `crm.erase_subject`, inactive clients auto-archived; one `lifecycle_run` row + one audit event per agency; skips anything under a legal hold or the delete block; DRY RUN (counts only) while `LIFECYCLE_DRY_RUN` is on (default). AA-969 d16: for the pilot agency (`crm.tenant.is_pilot`) only the trash purge (its own deletions) runs; the AA-initiated steps are skipped (`pilot_skipped_count`). Not deploy-gate exempt |
| `crm_offboard` | per owner step (queued in the request's transaction) | AA-942: `mode=export` builds and seals the agency export; `mode=erase` erases the agency, deletes its objects, destroys its keys, writes the receipt. Not deploy-gate exempt |
| `crm_dispatch` | per queued message (after the 10 s undo delay for a user action) | AA-942: the dispatcher gate at send time (trashed, archived, no marketing consent -> dropped; no sender -> held). Not deploy-gate exempt. AA-993 G4b (crm-023): a `crm.dispatch_attempt` row is committed **before** the provider call and closed with the decision; a run that finds an open attempt (stopped between the send and its decision) holds the row `send_uncertain` (message `failed` / `send_uncertain`) instead of sending again, and a run cancelled before the sender was called closes it `not_sent`. So it is **not deploy-blocking** any more (it was from G5 until G4b) |
| `crm_membership_pull` | 20 s, Recurring (next slot queued at each worker maintenance pass, about every 30 s when idle) | AA-978: pulls `<CIS_API_URL>/tenant/membership/v1/events` with an M2M token of scope `cis.tenant/read` (section "CIS membership feed"). Deploy-gate exempt and DB Migrate idle exempt (Infra `owners.yaml`), like the heartbeat; a CIS failure ends the run successfully with the error class in `crm.membership_feed_state` and backs off (20 s doubling to 5 min) |
| `crm_entitlement_pull` | every 2-minute slot, due-now hook (`app.cis.entitlement_feed.EntitlementPullSchedule`, key `crm_entitlement_pull:slot:<n>`) | AA-1028 R1 slice 2: pulls `<CIS_API_URL>/tenant/v1/account-events` and the snapshot of each agency an event names (section "CIS account feed"), plus an hourly pass over every agency. Not deploy-gate or DB Migrate exempt (a row exists only while due or running, seconds); a failing agency in the hourly pass is counted and skipped (`reconcile_failures`); a CIS failure ends the run successfully with the error class in `crm.entitlement_feed_state` and backs off. Counts only |
| `crm_pilot_backup` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook (`app.lifecycle.pilot.DailyPilotBackupSchedule`, key `crm_pilot_backup:schedule:<date>`) | AA-969 part 2 (root ADR 0019 d16): finds the pilot agency (`crm.tenant.is_pilot`, read in each agency's own `lifecycle_tx`) and queues one portability export of it (`crm.export_job` scope `tenant`, `requested_by = job:crm_pilot_backup`, + its `crm_data_export` job): the nightly per-agency backup, sealed under the agency `export` DEK at `exports/tenants/<tenant>/portability/<export>.zip.enc`, kept 7 days by the bucket rule. `busy` when an export of the agency is already queued or building; an owner's own agency export cancels an open backup and takes its slot. Left out of the owner's export list. Counts only. Not deploy-gate exempt |
| `crm_stage_date_rule` | hourly, due-now hook (`app.stages_job.HourlyStageDateRule`, key per UTC hour) | AA-943: per active agency, "today" in `tenant_settings.timezone`: booked -> travelling on the start date (reason `date_rule_gate_unmet` + an urgent task), pre_departure -> travelling, travelling -> completed on the end date, one `return_date_missing` task for a travelling trip without one. AA-946: booked -> travelling says `date_rule` when Stage 3 is complete (s3 = 100, recomputed) and `date_rule_gate_unmet` + the urgent task only when it is not; one `stage3_due_t21` task for a booked trip within 21 days of departure with s3 < 100; deletes e-mail codes older than 24 hours. Counts only. Not deploy-gate exempt |
| `crm_fx_fetch` | daily, due-now hook from 00:00 UTC (`app.fx.fetch.DailyFxSchedule`, key `crm_fx_fetch:<date>`) | AA-944: the platform FX rates in `crm.fx_rate_platform` (`ON CONFLICT DO NOTHING`, the only writer): `FX_PROVIDER=synthetic` a fixed USD table incl. VND (Dev), `live` ExchangeRate-API open access (USD base, VND crosses) + ECB (EUR base), fawazahmed0 stored as a cross-check only (a gap > 1 % is logged and counted). Counts only. Not deploy-gate exempt |
| `crm_mail_ingest` | per UTC minute, only while `raw/` of the ingest bucket holds objects (`app.mail.ingest.MailIngestScan` hook lists one key; payload `{"purpose": "mail_ingest"}`, never an object key) | AA-945: up to 50 forwarded mails per run: parse, `ingest_tx`, thread, store under the mail DEK, keep allowed attachments as documents, delete the raw object; an unknown address is deleted and counted (`MailIngestDropped`, card R). Not deploy-gate exempt |
| `crm_mail_health` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook (`app.mail.health.DailyMailHealthSchedule`) | AA-945: every agency's mailboxes: token refresh / SMTP login + NOOP; passing -> connected (held messages re-queued), revoked -> disconnected + one owner alert, unreachable -> needs attention. Counts only. Not deploy-gate exempt |
| `crm_mail_history_import` | per mbox / CSV upload (queued in the upload's transaction) | AA-945: parses the upload from `imports/<tenant>/<id>`, stores each mail through the ingest path (`imported`), counts on `crm.mail_import`, deletes the object. Not deploy-gate exempt |
| `crm_booking_sync` | 60 s, Recurring, plus a one-agency nudge per AA request (payload `tenant_id`, `aa_request_id`) | AA-947: sends the due `crm.aa_request` rows of every active agency to Booking (`<BOOKING_API_URL>/booking/b2b/v1`, one M2M scope per call; holds / confirms with a stable `Idempotency-Key`), applies the answers (line, money at the quote lock, `trip_commitment`, `waiting_on`), keeps a request pending with a backoff while Booking is down, then pulls the agency's `/events` into `crm.inbox_events` (section "CRM-19"). Deploy-gate exempt and DB Migrate idle exempt (Infra `owners.yaml`), like the membership pull; `not_configured` without `BOOKING_API_URL` / `M2M_CLIENT` |
| `crm_catalog_sync` | hourly, due-now hook (`app.catalog.sync.HourlyCatalogSync`, key per UTC hour) | AA-947: refreshes `crm.aa_catalog_tour` from `<CIS_API_URL>/catalog/v1` (scope `cis.catalog/read`): full passes comparing `content_hash`, a changed tour fetched with `If-None-Match`, tours no longer listed re-checked (410 retired, 404 paused); bounded per run, the pass continues next run; a CIS failure backs off. AA-951: after the tour pass, `crm.aa_content` from `/aa-content` (prep packs, starter templates; a body fetched only when version or hash changed, at most 60 a run; items CIS no longer lists unlisted after a complete list); a content failure is counted (`content_error_count`), never fatal. AA-1093: a state write that retires a tour (served / paused -> retired) or serves a retired one again queues one `crm_tour_retired_fanout` row in its transaction (`fanout_retired_count` / `fanout_reinstated_count`). Not deploy-gate exempt |
| `crm_studio_activation` | per accepted Content studio invitation (queued in the accept transaction, idempotency key per invitation, 12 attempts) | AA-979: CIS `tenant.membership.v1` 1.1 `activateInvitation` with an M2M token of scope `cis.tenant/activate` (`app/members/studio.py`, `app/cis/activation.py`); a CIS outage / 5xx / 401 / 403 retries with backoff, a CIS 404 / 409 fails it permanently with the code on the row. Not deploy-gate exempt |
| `crm_data_export` | per owner request (queued in the request's transaction, idempotency key per export, 3 attempts) | AA-967: builds one portability ZIP (`app/portability/builder.py`: every tenant table of the catalog minus the never-exported ones as JSON + CSV, the documents and `documents/manifest.csv`, `manifest.json` with the schema version, `SHA256SUMS`), progress per step in its own short transaction, sealed under the agency `export` DEK into the documents bucket, then `ready` (`app/portability/exports.py`). A coded refusal fails the row at once, an outage retries, the last attempt fails it `export_error`. Not deploy-gate exempt |
| `crm_client_import` | per slice of 500 rows of an owner's client import (the first queued in the start request's transaction, each slice queues the next, 3 attempts) | AA-962: reads the file from `imports/<tenant>/client-<import id>` in the ingest bucket, runs `app/imports/engine.py` with `apply=True` in chunks of 100 rows (one `system_tx()` each, the per-agency import lock, the intake's e-mail / phone dedupe locks), counts per outcome in the result (ids, codes and counts only), deletes the object after the last slice. A rerun or retry skips what is already in (`already_imported`). Not deploy-gate exempt |
| `crm_ops_release_sync` | hourly, due-now hook (`app.ops_release.job.HourlyOpsReleaseSync`, key per UTC hour), plus a one-trip nudge (payload `tenant_id`, `trip_id`) when Booking confirms / declines / cancels an AA line, a member withdraws or the trip is cancelled | AA-956: per active agency, "today" in `tenant_settings.timezone`: create / refresh / T-21 alert / withdraw the trip's ops release, then purge the due ones with a receipt (section "CRM-26 Module 8 ops release") |
| `crm_messaging_templates` | per connect / Refresh of an agency's SMS / WhatsApp connection (queued in the request's transaction, payload `tenant_id`) | AA-958: reads the agency's WhatsApp templates from its own Twilio account (Content API) into `crm.channel_template`; a refused token disconnects the connection. Not deploy-gate exempt |
| `crm_messaging_health` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook (`app.messaging.health.DailyMessagingHealthSchedule`) | AA-958: every agency's SMS / WhatsApp connection: account read + template sync; passing -> connected, refused -> disconnected + one owner alert, anything else -> needs attention. Counts only. Not deploy-gate exempt |
| `crm_workflow_run` | per workflow run: queued when it starts (after the 20 s Undo window for a member's action) and by the tick for a run whose wait ended (never queued ahead) | AA-959: `app/workflows/runner.py`: the run's due steps in one `system_tx` (actor `job:crm_workflow_run`); idempotent (a run not due or no longer active is left). Not deploy-gate exempt |
| `crm_workflow_tick` | hourly, due-now hook (`app.workflows.tick.HourlyWorkflowTick`, key per UTC hour) | AA-959: per active agency: date triggers (anchor + offset within the last 3 days), special dates (at 08:00-09:59 local: birthdays, passport expiry, trip anniversaries), one `crm_workflow_run` job per run whose `next_at` passed, task due dates from the trip's dates, the digest job at 07:00 local. Counts only. Not deploy-gate exempt |
| `crm_task_digest` | per agency and local day, queued by the tick (idempotency key per agency and date) | AA-959: the morning digest (counts only, AA SES) when `task_digest` is on. Not deploy-gate exempt |
| `crm_partner_accounts` | daily, from `JOB_RETENTION_UTC_HOUR`, due-now hook (`app.members.accounts.DailyPartnerAccountSchedule`), plus one run per deleted test agency (queued in the delete's transaction, payload `tenant_id`) | S263 (AA-939 follow-up): deletes never-signed-in partner-pool accounts that no member, live invitation or studio invitation needs (section "Partner-account clean-up"); counts only while `PARTNER_ACCOUNT_CLEANUP_DRY_RUN` is on; at most 50 deletes per run. Not deploy-gate exempt |
| `crm_tp_lead_pull` | every minute slot, due-now hook (`app.tripplanner.leads.LeadPullSchedule`, key `crm_tp_lead_pull:slot:<n>`) | AA-1038 R5 slice 3 (root ADR 0019 d11, contract `lead.submitted.v1`): per agency (`registry.agencies`), entered first (a `system_tx` that reads `crm.tenant_entitlement.tp_crm_leads`, crm-047: a locked / offboarding agency is skipped BEFORE the list call, so TripPlanner's per-tenant beacon goes stale and it refuses that tenant's crm leads; an agency whose CIS account does not entitle both `tp.hosted` and `crm.core`, projected by `crm_entitlement_pull`, is skipped with no HTTP call, `skipped_not_routed`), then `GET <TRIPPLANNER_API_URL>/trip/m2m/v1/tenants/{agency}/leads` with the M2M client (scope `tripplanner.leads/deliver`); each lead in one `system_tx` of that agency: another tenant's lead is refused (never stored, never acknowledged), the traveller's name / e-mail / phone are checked with the contract's one Traveller rule (`traveller_errors`, shared vectors `docs/contracts/rules/`), the answers are mapped onto the Stage 1 inquiry and validated like the hosted form (an optional answer that does not validate is dropped), stored through `store_submission` (idempotent on `tp-lead:<lead_id>`, the `crm_intake_process` job in the same transaction) under the agency's system form "Trip planner" (lead source `tripplanner`, disabled for the public, created on the first lead), then acknowledged (`stored` / `rejected` + reason) so TripPlanner redacts its copy; a rejected lead raises the ops alert `tp_lead_rejected`. TripPlanner's 404 `tenant_not_on_crm` / `unknown_tenant` is a counted skip (`skipped_not_on_crm`), not an error; any other TripPlanner or token failure is counted and the run ends successfully (next slot). One summary log line per run (`tp_lead_pull_summary`: agencies, pulled, skips, errors). `not_configured` without `TRIPPLANNER_API_URL` / `M2M_CLIENT`. Counts only. Not deploy-gate or DB Migrate exempt |
| `crm_pdf_render` | event: one per member's PDF request (`POST /trips/{id}/pdfs`, key per trip, kind, subject and minute) | AA-1070: builds the booking confirmation / invoice / proposal / voucher PDF of a trip (`app.documents.pdf_job`, our own PDF writer) in one `system_tx` (actor `job:crm_pdf_render`) and stores it as a sealed `crm.document` (uploader `system`); the subject gone or the agency locked = finished with a `reason_code`; without the key provider or the documents bucket `not_configured` (never a queued-ahead retry). Ids only. Not deploy-gate exempt |
| `crm_tour_retired_fanout` | event: one per AA tour transition the catalog sync stored (key per tour, transition and `state_changed_at`) | AA-1093 part 1: for every agency in its own `system_tx` (actor `job:crm_tour_retired_fanout`): `retired` raises `crm.tour_retired_flag` rows for each live trip's AA options of draft / published / sent versions and live AA lines on the tour, plus one task `aa_tour_discontinued` per trip (the trip owner, else the agency owner; never a write on a line, option or proposal); `reinstated` clears the open flags and adds one internal note to a trip whose open task stays; a row whose tour moved on finishes `stale`. Ids and counts only. Not deploy-gate exempt |
| `crm_job_retention` | daily, from `JOB_RETENTION_UTC_HOUR` (default 19 UTC), due-now hook | Data standards §7 row `crm.job`: succeeded 30 d, cancelled 90 d; **failed rows kept** until an archive bucket exists; DRY RUN (counts only) while `JOB_RETENTION_DRY_RUN` is on (default) |


### Ops checks (S263 alert diet, AA-1025)

`app/observability/ops_alerts.py` replaced the CRM CloudWatch alarms (root `docs/audits/2026-11-01-S263-alerting-cost/
diet.md`). The job sampler (`app/jobs/metrics.py`, every 5 minutes, API process) evaluates the **state checks**
`jobs_stuck` (due row > 900 s, no healthy job running), `jobs_overdue`, `audit_export_lag`, `audit_partitions`,
`audit_verify_lag`, the CRM's daily AI budget `llm_app_spend_near` (80 %) and `llm_app_spend_cap` (100 %; AA-1025 H1c,
`app_profile.read_day_spend`, only when the app profile sets a daily budget), and drains the **events** the code raises with `ops_alerts.emit(check, scope, **ids)` next to the
log lines it already wrote: `breakglass_opened`, `breakglass_owner_not_mailed` (per session), `ops_release_incomplete_t21`,
`ops_release_withdrawn` (per release), `membership_apply_refused` (per event), `membership_feed_stuck` (per day),
`bulk_reveal` (per member and hour), `pilot_backup_failed` (per export: a failed nightly `crm_pilot_backup` export, S269), `tp_lead_rejected` (per lead: a TripPlanner lead the CRM acknowledged rejected, AA-1038). Each finding is e-mailed once on the platform alerts SNS topic (env
`ALERTS_TOPIC_ARN`, task role `sns:Publish`, both AA-CIS-Infra), deduped by a ledger row in `crm.job` (kind
`crm_ops_alert`, status `succeeded`, idempotency key `crm_ops_alert:<check>:<scope or UTC day>`, payload ids only;
`queue.record_alert` / `queue.forget_alert`). The ledger rows are never claimed and are not busy work for the deploy
gate or DB Migrate. A test keeps every event check raised somewhere in `app/` (`tests/unit/test_ops_alerts.py`). The
service being down is the Infra alarm `aa-crm-<env>-no-healthy-task`.
## CI and release

- Web checks (AA-935): **Web Build** (kit core integrity + provenance, vitest, `next build` with the type check,
  standalone smoke, kit lint) and **Web Docker Build Check** (native arm64 image, container smoke, Trivy); Security Audit also runs `npm audit` for `web/`.
- Portal checks: retired with `portal/` (AA-1036 R3 slice 2): "Portal Build", "Portal Docker Build Check" and
  `deploy-portal-dev.yml` are gone; the studio is built, linted and smoke-tested as part of the web.
- **Kit lint** (AA-994 S2b, root ADR 0016 decision 7): `web/` (and `portal/` until AA-1036) runs ESLint 9 with the Next rules plus
  the kit preset `inline` from the vendored `app/_kit/core/eslint-kit.mjs` at `error` (`eslint.config.mjs`; portal:
  `app/**`, web: `app/**` + `components/**`, the vendored core exempt). Violations that existed before S2b are frozen
  in each app's `eslint-suppressions.json` (ESLint bulk suppressions), which only shrinks: the job runs
  `eslint --prune-suppressions`, fails when the committed file differs from the pruned one, fails when an entry grows
  against the base branch (`check-kit-copy.mjs --suppressions … --base-files …`) and fails on an inline disable of a
  kit rule or a bare `eslint-disable` in page code (`--no-kit-disable`). After fixing violations run
  `npx eslint --prune-suppressions` in the app folder and commit the file; never `--suppress-all` to hide a new one.
  The steps are inside **Portal Build** / **Web Build** (required checks), so they gate the merge; the pre-push hook
  runs the dependency-free kit checks and ESLint on the changed files (`scripts/pre-push.sh`).
  AA-1008 (S6f): `web/eslint-suppressions.json` is empty (`{}`): the studio's legacy layer (`portal/_components/ui.tsx`)
  is gone (kit twins where the kit has one, the rest in `web/app/_kit/local/studio/StudioCard.tsx`), raw tables are
  the kit `DataTable` (`rowTestId` keeps row test ids), the stage-engine dialog is the kit `Modal` (`pinFooter`), and no
  effect sets state synchronously. New code adds no entry.
- **Visual run** (AA-1008, root ADR 0016 decision 9; `.github/workflows/visual.yml`, `e2e/visual/`,
  `e2e/playwright.visual.config.ts`; report-only until hand step H3): per PR, the web is built and started in the
  runner (`KIT_GALLERY=1`, the Web Build smoke's fake configuration) and every changed public page plus the gated kit
  gallery (`/kit-gallery`, 404 on every deployed build) is captured at 1440 / 390, light / dark: no horizontal overflow
  of the document or a kit frame, 0 console errors, no failed same-origin request; a planted-overflow self-test must
  fail first. After a green **Deploy Web Dev** on main (`workflow_run`) the changed pages are captured on the agency host,
  CRM and studio pages signed in as the e2e studio user (a page that session is sent away from is reported skipped).
  `web/tests/visualPages.test.ts` (G16) fails when a route is neither listed in `e2e/visual/pages.ts` nor excluded with a
  reason. Screenshots and `report.json` are the run's artifact.
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
- Post-deploy verify of the CRM web with the Content studio (AA-993 V3, AA-1036): the same workflow with input `app`
  (`crm` default or `crm-web`; optional `resolve_via` pins the host to a CloudFront name), called by
  `deploy-web-dev.yml` after `deploy`. Rows `.github/verify/crm-web.rows` (`/healthz` `git_sha`/`version`/
  `auth_configured`; `/crm/health` + `/crm/health/ready` reaching a ready API through the host's API rule, without
  comparing the API's commit: a web deploy does not deploy the API; the merged studio's `/api/healthz`
  `service: "aa-crm-web"`, `studio: "merged"`, `sha`/`version`), plus `.github/scripts/edge_verify.sh crm-web`
  (signed-out redirects to the web's `/auth/refresh`, `/legal/terms` reaching the CRM web (AA-1033), "/" ->
  `/portal/dashboard`, `/tenant-login` 308 to the partner sign-in, `/terms` 308 to `/legal/partner-terms`, that page
  and its `/_next` asset). Same red rules (desired 0, rollout, image). `CRM_WEB_ENABLED` unset is red (`config` fails).
  The web deploy then runs the studio's Playwright smoke (`ui-smoke-portal.yml` on the agency host,
  `require_tenant_login: true`: a missing e2e studio sign-in secret `E2EPORTALEMAIL` / `E2EPORTALPASSWORD` /
  `E2EPORTALTOTP` is red). Read **Deploy Web Dev result**. The `crm-portal` app went with the studio service (AA-1036).
- release-please (manifest mode) keeps one release PR; the version is baked into the image and returned by
  `/crm/health`.

## Web (`web/`, AA-935)

- **Hosts** (`web/lib/hosts.ts`, `web/proxy.ts`): the partner host serves the agency app; the client host
  (`WEB_CLIENT_HOSTS`, later `go-dev.`) serves `/c/*` and `/trip/*` only (both 404 on the partner host); any other
  Host is 404; `/healthz` answers any Host (the ALB health check sends the task IP). Cookies are host-only, so the two
  hosts never share a session.
- **Book now on the trip page** (AA-1072): a link with an agency's inquiry form key (`/trip/<tourId>?f=<key>[&p=<AA tour
  key>]`) shows that agency's booking widget (`web/components/booking/BookNowWidget.tsx`, `web/lib/bookNow.ts`; words
  EN by default, VI by the visitor's cookie, `web/lib/i18nBookNow.ts`) instead of the AA contact button; section "Book
  now and online payment".
- **Trip page** (AA-979 P2b §4.3, `web/app/trip/[tourId]`, `web/lib/tripPage.ts`): the public AA tour page moved from
  the CIS admin host (which answers 307 to it). Server side it reads CIS's public `GET <CIS_API_URL>/v1/trip/{id}`
  (no token; CIS stamps `url_alive` for its T10 gate): CIS 301 (superseded version) -> 308 to `/trip/<current id>`,
  410 / 404 -> not-found page, a CIS fault or an unset `CIS_API_URL` -> 500. Indexable (page metadata overrides the
  root `noindex`; `public/robots.txt` allows `/trip/` only). Next data cache 1 h.
  AA-1033: under the AI-written summary, highlights and itinerary it shows the kit `AiNotice` traveller line
  (English; "may contain mistakes … confirmed in your official quote").
  **AA-979 P2b window**: the agency host `portal.` is a partner host too (`WEB_PARTNER_HOSTS` lists both
  `partner-dev.` and `portal.`), and each partner host answers with its OWN public origin (`lib/config.ts`
  `forHost` / `requestConfig`): redirects, the OAuth redirect / logout URIs and the same-origin write guard stay on
  the host the person is on, so the DNS cutover of `portal.` needs no web or API change. `WEB_PUBLIC_ORIGIN` is the
  fallback for any other Host. On `portal.` the Content studio link is the same-origin `/portal/dashboard`, a person
  with no CRM agency goes straight to it (a Content-studio-only member), and sign-out also expires the studio's
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
  Adding a browser call = adding one line there. One literal route beside the catch-all: `GET /bff/identity`
  (`web/app/bff/identity/route.ts`, AA-1025 S264) answers the Content studio's "who is signed in" ALWAYS with 200
  (`signed_out` / `no_agency` / `agency` with only `display_name` + `member_role` / `unknown`), because the studio
  page must not log a console error; `/bff/me` keeps its 401 / 409 for the CRM pages.
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
  `data-card`), the Content studio cross-link (`CONTENT_STUDIO_URL`), Settings with language and dates. The
  cross-link shows only when CIS `GET /auth/tenant-memberships` (the person's partner access token, `CIS_API_URL`)
  lists the current agency among the person's studios; kept when CIS cannot answer; memoised 5 min per person and
  agency, CIS throttles that route (`web/lib/studioAccess.ts`, AA-1025 S264). The portal
  sidebar (`portal/`, AA-979) links back ("Open CRM").
- **UI kit** (AA-994 S2a, root ADR 0016): `web/app/_kit/core/` is a byte copy of AA-CIS-App `frontend/app/_kit/core/`
  (full profile), written only by the root `tools/kit/kit-sync.sh crm-web` and pinned by `web/kit.lock.json`; never
  edit it here. `web/app/_kit/local/` holds the CRM's own parts (the theme binding, key `cis_theme`, and `CrmShell`);
  `web/app/_kit/index.ts` is the CRM's barrel, which pages import. The `--aa-*` palette is `core/tokens.css`, imported
  by `app/layout.tsx` before `globals.css`; no other CSS file may define an `--aa-*` variable. Web Build checks
  integrity (`core/check-kit-copy.mjs`, also `--css`) and provenance (`api/scripts/check_kit_provenance.py`: the copied
  manifest is CIS's at the lock's `ref`, and that `ref` is on CIS `main`). `web/lib/{sameOrigin,crossSiteGuard,bffPath,
  proxyResponse}.ts` stay byte copies of AA-CIS-App `frontend/lib/` (CI "Vendored CIS copies").
- **Theme and hydration** (S262): `app/layout.tsx` renders no `data-theme` (the restore script alone sets it before
  paint) and mounts `_kit/local/ThemeKeeper`, which applies the stored choice again whenever React rebuilds the
  document (a failed hydration re-renders the root on the client and drops `<html>` attributes). Times on agency pages
  print in the agency zone (`formatInstant`), never the server's or the browser's, so server and browser agree.
- **Codes the UI words**: every `crm.task` kind and stage-history reason the API writes is listed in
  `api/app/task_kinds.py` (API unit test: complete against the code); `web/tests/tripCodes.test.ts` fails when one
  has no EN or VI label. A new task kind = one line there + `task.<kind>` in both languages.
- **Configuration** (task environment, AA-CIS-Infra `crm_web.tf`; meanings in `web/lib/config.ts`): `ENVIRONMENT`,
  `WEB_PUBLIC_ORIGIN`, `WEB_PARTNER_HOSTS`, `WEB_CLIENT_HOSTS`, `CRM_API_ORIGIN`, `COGNITO_DOMAIN`, `COGNITO_CLIENT_ID`,
  `CONTENT_STUDIO_URL`, `CIS_API_URL` (AA-979, the trip page; optional), and from Secrets Manager
  `COGNITO_CLIENT_SECRET`, `WEB_SESSION_SECRET`. AA-1019 adds the staff console's four `STAFF_*` values (section "AA staff
  console"). Missing identity values
  turn sign-in off (the pages say so) while `/healthz` stays up.
- **Deploy**: `.github/workflows/deploy-web-dev.yml` (same model as the API deploy, no job guard: the web runs no
  worker), gated by the `dev` environment variable `CRM_WEB_ENABLED` (unset = red), then the post-deploy verify
  (`app: crm-web`, section "CI and release").

- **AI notice and terms (AA-1033)**: the kit `AiNotice` (AA-CIS-App canonical core, wording in
  `core/aiNotice.messages.ts`) is the one notice under AI-written text. CRM web: every component or page that asks for
  an AI draft (`trips/{id}/ai/draft`) must render `<AiNotice lang={lang} />` (staff variant) under the draft —
  guarded by `web/tests/aiNotice.test.ts` (the trip workspace's AI draft drawer, AA-1027 F3, renders it). Settings > AI
  lists "AI can make mistakes: check facts before sending." first, shows the notice as a draft carries it and links
  `/legal/terms#ai`. `/legal/terms` (`web/app/(crm)/legal/terms`, strings `web/lib/i18nTerms.ts` EN + VI, signed-in,
  linked from the shell footer; never `/terms`: on the agency host the ALB sends `/terms`, `/privacy` and `/assets/*`
  to the Content studio, so the path and every link come from `web/lib/legal.ts`, guarded by
  `web/tests/crmTermsPath.test.ts` against the agency-host rule patterns) is a draft holding the "AI-generated content" section only (mistakes, for information, a person
  reviews and sends, the confirmed quote and booking terms bind; no liability waiver; counsel reviews later). Privacy
  wording there and in Settings > AI describes the F3 scrubber as it works (the trip's known details plus what it can
  detect, may not catch every detail), never an absolute promise.
  Portal: the `publisher` variant replaces the local `AiContentNotice` at review, publish and export, same sentence.
- **Content only, final read before publish (AA-1088, S278 audit M3 / M6 / M9)**: the My Content piece drawer
  (`ReviewDrawer.tsx`) shows only the agency's own text and the AI notice; the writer's brief (angle formula / style,
  source fact) and the search context (people-also-ask, related terms) are not shown there (the Create wizard still
  shows the angle cards the agency picks from). CIS refuses the first publish of a piece it could not fully confirm
  against the trip (409, body `code` `content_needs_final_check`, `can_publish_anyway`; the BFF drops headers, so the
  code is read from the body: `web/lib/studio/publishCheck.ts`). The studio shows its own EN / VI sentence
  (`errors.messages.ts` `needsFinalCheckOwner` / `needsFinalCheckEditor`); on Publish the account owner gets
  **Publish anyway**, which sends `{"publish_anyway": true}` (CIS audits it). The draft legal pages (Draft v0.7)
  describe the partner sign-in (each person's own sign-in with a second step, roles set by the owner), never an API key;
  `tests/studio/studioLegalProviders.test.ts` keeps "API key" out. Tests: `tests/studio/prepilotFixes.test.ts`.
- **AI-content mark (AA-1040, EU AI Act Art. 50)**: Content studio shows the kit badge form
  (`<AiNotice variant="publisher" form="badge" />`, "AI-assisted", the publisher sentence as its accessible name) under
  the channel of each My Content and Published row, in the piece drawer title and on each publish card. The
  machine-readable mark of a piece lives in CIS (`content_piece.writer_input -> 'ai_provenance'`, `piece["ai"]`,
  `data-ai-*` / `<meta name="ai-*">` in the WordPress post). The studio BFF (`/api/tenant/*`) passes the CIS
  `X-AI-Generated` / `X-AI-Generator` / `X-AI-Model` headers and a checked `Content-Disposition` on top of
  `Content-Type`, rebuilt from an allow-list (`web/lib/studio/downloadHeaders.ts`); every other upstream header is
  dropped. The T2 DOCX carries its mark in its core properties and, since AA-CIS-App #1366 (S272), the same `X-AI-*` headers. CRM AI drafts: `POST trips/{id}/ai/draft` answers
  `ai: {ai_generated, generator, generated_at}` (null without model text; the model is on the ledger row
  `llm_call_id`) and the draft text element carries `data-ai-generated` / `data-ai-generated-at`. A message a member
  writes from a draft is their own (crm.message carries no mark).

## One agency app (`web/`, AA-1036 R3, root ADR 0019 d6 / d7)

The Content studio is part of the agency app: one Next.js app, one partner-pool sign-in (MFA), one session. Slice 1
copied the studio into `web/` and left `portal/` and its deploy untouched. Slice 2 cut over (the agency-host studio
paths `/portal*`, `/api/*`, `/tenant-login*`, `/terms`, `/privacy`, `/assets/*` reach the web; the web task has
`WEB_CONTENT_HOME=true`; AA-CIS-Infra `crm_agency_edge.tf`, `crm_web.tf`), then retired the separate studio: `portal/`,
its CI jobs and deploy, and in AA-CIS-Infra its ECS service, task definition, target group, ECR repository, roles and
studio rules. Deploy Web Dev verifies the studio paths and runs the studio smoke. Implementation notes: `docs/implementation-notes/AA-1036.md`.

- **Layout**: route group `app/(studio)/` (`/portal/*` with the same URLs, `/portal-access/*`, the draft legal pages
  `/legal/partner-terms` and `/legal/privacy`), the studio BFF at `/api/*` (`app/api/tenant/[...path]`, `tenant/me`,
  `auth/tenant-logout`, `auth/tenant-select`, `healthz`, `studio/apps`), the shell under
  `app/_kit/local/studio/`, helpers under `lib/studio/`. One kit copy (`web/app/_kit/core`). The studio stylesheet
  (`app/(studio)/studio.css`: Tailwind 4 preflight with `source(none)`, no utility, plus the studio rules) loads only
  under the group; links between the studio and the CRM are plain `<a>` (full page loads).
- **No tenant API keys (R3 slice 3)**: CIS answers `POST /auth/tenant-login` with 410 Gone and has no
  `GET /v1/account/api-key`; the studio has no API page (`/portal/api` and `/portal/account/api` answer 308 to
  `/portal/account/settings`), no API key card on Integrations, no API playground or its BFF, and the smoke signs in
  with the partner pool everywhere (no `E2E_TENANT_API_KEY` path). `test_studio_guards.py` keeps the studio free of
  `/auth/tenant-login`, `/v1/account/api-key`, `X-API-Key` and the key card. A machine API for agencies is R8.
- **Gate order** (`proxy.ts`): host -> `/staff` -> `/c` -> studio -> CRM. `/tenant-login*` answers 308 to
  `/auth/refresh?next=/portal/dashboard` (no people-facing API-key sign-in, no `PORTAL_API_KEY_SIGNIN`); `/api/*`
  passes (handlers answer 401 JSON); the studio legal pages are public; `/portal*` needs the session, then `studioGate`
  makes sure a CIS studio token bound to this sign-in (`cis_partner_ref`) exists: CIS `POST /auth/tenant-session`
  (server side, `CIS_API_URL`; at most 15 min, `amr=partner`), several studios -> `/portal-access/select`, none ->
  `/portal-access/none`, CIS down or unset -> none (fail closed). Only `amr=partner` tokens are ever used. The studio
  reads the app's own `lib/session.ts`: no byte copy of `session.ts` / `seal.ts`. `/terms`, `/privacy` and the old
  T-number slugs answer 308 (`next.config.ts`).
- **Content is home** (d7): with `WEB_CONTENT_HOME=true` the bare host "/" answers 307 to `/portal/dashboard`; the CRM
  home is also `/home` (the sidebar's Home). On since the slice 2 cut-over; the studio's CRM entry links `/home`.
- **App switcher** (`lib/apps.ts`): Content, CRM, Trip planner. An add-on the tenant holds opens; any other leads to
  **Account › Add-ons** (`/portal/account/add-ons`, AA-1036 R3), labelled by its CIS state (request / requested / on).
  The CRM entry comes from `GET /crm/v1/me` for the studio's tenant (`crm_entitled`, `app.entitlements.crm_entitled`:
  false only when the CIS projection put the agency on plan `free`), then the CIS add-on state; the Trip planner from
  CIS only (`GET /v1/account/add-ons` with the verified studio token). Studio: `GET /api/studio/apps` (always 200)
  feeds the sidebar's "Apps" group and says `crm_member` (the person holds a CRM role: `/crm/v1/me` answered 200);
  CRM: the former Content studio group is "Apps" (the studio link when CIS lists the agency for the person, and for
  the agency **owner** the Trip planner entry to the CRM's own **Settings › Add-ons** (`/settings/add-ons`); the group
  is hidden for a non-owner without studio access). Until S269 the entry led to the studio's Add-ons page, which a
  CRM-only owner cannot open ("No Content studio access").
- **Add-on requests** (AA-1036 R3, root ADR 0019 d7): the Add-ons page shows each add-on's state, the pending request
  and the last decision (a decline with its reason), and lets the Content studio **owner** send a request with an
  optional note (`POST /api/tenant/v1/account/add-on-requests` → CIS; CIS refuses editors and viewers, and the page
  shows them why: `/api/tenant/me` names the role from the verified token, display only). The request is stored in CIS
  (`shared.tenant_addon_request`), rings the AA staff bell and is approved (the plan attached through the CIS
  `tenant_provision` saga) or declined in CIS Tenant 360 › Subscription by an admin or the account manager. No mail, no
  vendor. An active CRM shows "Open CRM" only to a person with a CRM role (`crm_member`), else "Ask your owner for CRM
  access"; an approval whose setup still runs or failed says so (CIS `setup`); a refusal reads CIS's error code
  (`ApiError.code`: 409 `tenant_not_in_service` vs `already_subscribed`).
- **CRM add-on requests** (AA-1036 R3, S269 live gap): an agency whose owner works in the CRM without a Content studio
  membership asks from the CRM's **Settings › Add-ons** (`web/app/(crm)/settings/add-ons`, kit Section / Badge / Field /
  Textarea / Button). The CRM API (`app/routers/addons.py`, owner only: `PLAN_VIEW` to read, `ADDON_REQUEST` to ask)
  relays to CIS `tenant.addons.v1` (`app/cis/addons.py`, M2M scope `cis.tenant/addons`, the CRM's CIS client config
  `app.state.studio_activation`): the same list and the same request as the studio's, so the requests keep one store
  in CIS and one staff decision path; CIS records the requester as `crm-member:<member id>`. No connection is held
  across the CIS call (role check in one `tenant_tx()`, CIS call, outcome in a second short `tenant_tx()`); the agency
  audit has `addon_request` allowed (object = the CIS request id, reason `<product>_created` / `<product>_open`) or
  denied (reason `cis_<CIS reason>`). CIS unreachable, not serving the contract yet or the scope not granted: 503
  `addons_unavailable`; no CIS account: 404 `addons_no_account`. The CRM stores nothing of the request.
- **AI paused banner** (AA-1036 R3): `GET /crm/v1/me` answers `ai_paused` (true while `crm.llm_app_profile.ai_enabled`
  is false: AA switched the CRM's AI off for the whole app from the CIS admin's App profiles; never the staff reason);
  the CRM layout shows the amber `components/ai/AiPausedBanner.tsx` on every page while it is.
- **EN/VI** (AA-1036 R3): the studio speaks English and Vietnamese with the kit i18n (`app/_kit/core/i18n.ts` +
  `useI18n.ts`; message files `*.messages.ts` next to the code, `defineMessages(en, vi)`). The language is the app's
  one setting (cookie `aa_crm_locale`, section "EN/VI and dates"; English by default): `app/(studio)/layout.tsx`
  resolves it per request and hands it to `app/_kit/local/studio/StudioLang.tsx` (a fixed kit `LangStore`,
  `lib/studio/lang.ts`: the cookie is httpOnly). The switch is the CRM's `LocaleSwitch` in the studio's account menu
  (the page reloads in the chosen language). Pure helpers take the language (`lib/studio/*.ts`, words in
  `lib/studio/lib.messages.ts`). The draft legal pages and the Facebook guide are structured per-language documents
  (`legal/*/<name>.messages.ts`, `help/facebook/facebookGuide.messages.ts`); the English legal text applies, the
  Vietnamese page says it is a translation. Text from APIs (goal names, notification titles, live-writer step labels,
  activity labels, generated content) is shown as the API sends it. `web/tests/studio/studioI18n.test.ts` keeps every
  English key paired with Vietnamese and fails on hard-coded English in studio components.
- **Own e-mail** (an AA-1007 leftover): `/auth/callback` keeps the id token's `email` (same client and `sub`,
  `lib/oauth.ts` `idTokenEmail`) in the sealed session (`em`, kept across refreshes); `/bff/identity` adds it to its
  signed-in answers, and the studio shows "Signed in to AA Partner as <email> (<Role> of <agency>)". Never an e-mail
  from an API answer.
- **Health and verify**: `/healthz` is the agency app's; `/api/healthz` answers `{ok, service: "aa-crm-web", studio:
  "merged", version, sha}`. Since slice 2 the crm-web verify reads it (`crm-web.rows` `studio-healthz`; the old
  service's `aa-partner-portal` is red) and `edge_verify.sh crm-web` checks "/" -> `/portal/dashboard`, the signed-out
  studio redirect, `/tenant-login` 308, `/terms` 308 -> `/legal/partner-terms`, that page and its `/_next` asset.
- **Sentry**: `instrumentation*.ts` covers the whole app. The browser DSN is the `NEXT_PUBLIC_SENTRY_DSN` build arg
  (Deploy Web Dev passes the `dev` variable `WEB_PUBLIC_SENTRY_DSN`, empty = off); the server `SENTRY_DSN` waits for a
  web DSN secret in AA-CIS-Infra (follow-up).
- **Guards**: `api/tests/unit/test_studio_guards.py` (the studio rules of `test_portal_guards.py` on the new paths),
  `web/tests/studio/*` (vitest), the Web Build standalone smoke, and the suppressions growth check, whose base gets the
  base branch's `portal/` entries under their new paths (`.github/scripts/studio_suppressions_base.py`) while the
  move is in flight.

## Tenant portal (`portal/`, AA-979 CRM-P2a) — history

**Retired in AA-1036 R3 slice 2.** The studio code lives in `web/` since slice 1 (section "One agency app"): the
`portal/app/portal/**` pages are `web/app/(studio)/portal/**`, `portal/app/_kit/local/` is
`web/app/_kit/local/studio/`, the BFF keeps its `/api/*` paths under `web/app/api/`, the studio helpers are in
`web/lib/studio/`, the draft legal pages are `/legal/partner-terms` and `/legal/privacy`. `portal/`, its CI jobs, its
deploy and the Fargate service `aa-crm-dev-portal` are deleted. The text below describes the separate app as it was;
read its `portal/...` paths through that map.

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
  Tenant API keys were removed in AA-1036 R3 slice 3 (section "One agency app"). The studio token lives in the host-only cookie `cis_tenant_token`; the gate is
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
  A local run sets `PORTAL_TARGET` empty and signs in with the same partner-pool values (AA-1036: no API-key path).
- **Account pages (AA-761)**: Billing = plan card, rewrite meter (used / quota and reset date from CIS `GET /v1/quota`,
  overage so far from `GET /v1/billing`) and an invoices empty state (CIS stores no invoices yet); Activity = kit
  DataTable with server paging over CIS `GET /v1/account/activity` (type and UTC-date filters) and a CSV export of the
  whole filtered set (`GET /v1/account/activity/export`); Integrations = each channel's health and test (Publish only
  shows a summary that links here; the API key card went with the tenant API keys, AA-1036). Pure helpers in `portal/lib/account.ts`, tested by `npm test` (node --test, in Portal Build).
- **Put back online (AA-1039 option A, S275)**: when AA serves a retired tour again, a WordPress page the retirement
  moved to draft stays in draft until the tenant clicks. Publish (`/portal/content/publish#put-back`) shows
  "Pages to put back online" (`PutBackList`, CIS `GET /v1/publish-log/put-back`; hidden when empty or refused), and the
  bell's `tour.pages_to_put_back` row lists the same pages (`PutBackNote`); each "Put back online" sends one CIS
  `POST /v1/publish-log/{publish_id}/put-back` (`putBackApi.ts`) and shows the outcome: online again, already online,
  or changed on WordPress and left as is. Nothing is published without that click.
- **Posts about tours no longer offered (AA-1039 follow-up, tech lead decision S276; root ADR 0019 d20 amended)**: when
  AA retires a tour, nothing changes on the agency's site by default. Publish (`/portal/content/publish#retired-pages`)
  shows "Posts about tours no longer offered" (`RetiredPagesList`, CIS `GET /v1/publish-log/retired-pages`; hidden when
  empty or refused): each WordPress page has one "Move to draft" (CIS `POST /v1/publish-log/{publish_id}/move-to-draft`,
  `retiredPagesApi.ts`; a repeat click answers "already a draft"), a Facebook post says to edit it on Facebook. The
  bell's `tour.pages_to_review` row lists the same WordPress pages with the same button (`ReviewPagesNote`, its own EN /
  VI sentence). Account › Settings has the owner-only opt-in "Automatically move posts about a discontinued tour to
  draft" after N days (`TourRetirementSettingsCard`, CIS `GET` / `PATCH /v1/account/tour-retirement`, 1-30 days,
  default 7, off by default; hidden when the read fails); a page of an opted-in agency shows its draft date. A page
  drafted either way is put back with "Put back online" after a restore.
- **Marketplace (AA-671)**: one page at `/portal/tours` for the tours a tenant can pick and the tours it picked (was
  two pages, Browse Tours and Tour overview). View "All tours" (default): CIS `GET /v1/tours/pool` as a card grid with
  server paging and filters (name, country, length; newest first, "New" for the last 14 days), the kit Drawer for one
  tour and the (bulk) rewrite with the Billing quota note (`lib/tours.ts` `rewriteQuotaNote` over `usageMeter`). View
  "In my catalog" (`?view=mine`): CIS `GET /v1/marketplace` (price, status, version); a card opens the tour in My Tours.
  AA-1029: the "All tours" card and drawer show AA-Booking's "from" price (contract `price.from.v1`, the pool row's
  `from_price`, read by CIS) as "From US$X per person" with the TripPlanner caveat, and no price without one; the
  source price text `price_raw` is never shown as a price (root ADR 0005 d7). AA-Partner reads no `booking.*` itself.
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
  /auth/tenant-session` for a tenant JWT of at most 15 minutes in the same `cis_*` cookies; several studios ->
  `/portal-access/select`, none -> `/portal-access/none` (both show the CRM sign-in, read live from the web's
  `GET /bff/identity` on the same origin, always 200, and Adventure Asia's address only labelled as a contact, AA-1025 S264); no
  session -> the web's `/auth/refresh`. The BFF renews an
  expiring exchanged token itself and answers 401 + `x-aa-session: expired` when it cannot (the shell reloads once).
  One sign-out: the studio clears its cookies, then posts the web's `/auth/sign-out`. The studio cookies are bound to
  the partner sign-in they were exchanged for (`cis_partner_ref` = 16 hex of SHA-256 `iss|sub`, AA-1025 S264): the
  gate and the BFF never use a studio token made for another person than the CRM web's live session (exchange again,
  without the old agency choice), so a second sign-in on the same browser never sees the previous person's studio. Sentry (`instrumentation*.ts`):
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
- **LLM gateway** (`app/llm/gateway.py`, D12): per-agency rate cap -> month budget (`tenant_settings.ai_budget_usd`)
  and `ai_enabled` (default off), before the paid scrub -> scrub receipt (guardrail rows) -> budget again when the
  scrub wrote guard rows -> seal the placeholder map -> call -> one `crm.llm_call` row (no text) + the rollup ->
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
  future call slots cancelled, links revoked); AA-957 completes it (section "CRM-27" below).
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
  -> cancelled (AA-1069: planning -> requested -> confirmed, see the supplier requests section). Confirm (`POST .../confirm`) runs `crm.booking_line_confirm` (SECURITY DEFINER): the agency's level
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

## CRM-27 commission + one-action cancel (AA-957)

Design v3 M13, C12 (§2.7, §3.5 "Cancellation"), R17, R21, A-12; lane §2.7 `commission`, §3.1, §3.4; migration crm-035
(contract). Code: `app/commissions/` (service), `app/routers/commissions.py`, `app/cancellation.py` (called by
`app.stages.cancel` and the Booking sync), `app/booking/sync.py` (`cancel` requests); decisions in
`docs/implementation-notes/AA-957.md`.

- **Commission rows** (`crm.commission`): one live row per confirmed agency line with a positive expected commission,
  written only by `crm.commission_sync(trip)` (SECURITY DEFINER: the assistant may confirm a line, R18, but reads no
  money), run after every confirm, money change and line cancel, by the trip cancel and by the hourly date rule
  (`commissions.sweep`). The expected amount is the head of the line's `commission_expected` chain (frozen at the
  confirmation day); never copied until the receipt. Status expected -> claimed -> received, or written_off /
  cancelled (line cancelled, commission set to 0, trip cancelled); claimed / received block the trip delete
  (`crm.trip_commitment` kind commission). Due date = last service day + the supplier's `commission_due_days`
  (default 30, Suppliers form). Overdue is derived; the date rule opens one owner task `commission_overdue` per row.
- **Receipt** (R17 E): `crm.commission_receive` freezes it at the rate of the day it arrived (`fx_basis
  receipt_date`; 409 `fx_rate_missing` without a rate); `short_amount` only in the original currency;
  `fx_difference_base` only when the original amounts match.
- **Routes** (`/crm/v1`, owner / agent: `VIEW_MARGIN` reads, `PAYMENT_MARK_RECEIVED` writes; the assistant 403):
  `GET /commissions?status=open|overdue|received|written_off|cancelled|all&trip_id=` (base-currency totals), `GET /
  PATCH /commissions/{id}` (due date), `POST /commissions/{id}/claim|unclaim|receive|write-off`. The RESTRICTIVE
  policy `commission_margin_roles` hides every row from the assistant and AA staff. The trip's lines carry
  `commission` (owner / agent). Success metric `commission_unclaimed` (Module 13) is live.
- **Cancel run** (`app/cancellation.py`): the dry run lists every effect and the detail blocks (AA bookings with
  Booking's dry-run fee read outside the transaction by the route, supplier lines, payments with the refund the
  dialog proposes, expected commissions, the accepted booking terms). The run: queued sends and workflow sends
  cancelled (`register_cancel_hook` for Module 10), open tasks skipped (except `verify_transfer`), slots and links
  cancelled, unpaid invoices voided (and with them the due balance rows of the schedule no kept invoice bills, S262;
  the hourly date rule voids the same rows, with their drafts, on a trip cancelled before that,
  `app.cancellation.void_cancelled_schedule`), one `proposed` refund request per payment for the amount the member set (lower
  keeps the rest; a payment without an invoice gets the owner task `cancel_refund_review`), the owner task
  `cancel_with_suppliers` (one per trip: the trip page lists each supplier on it as one checklist line; closed by the
  date rule once no confirmed agency line is left), expected commissions
  cancelled unless `keep_commission`, AA holds released, ops release withdrawn, one `cancel` request per AA booking.
- **A cancelled trip bills no more** (S262 live finding, §2.7 items 3-4): once a trip has a `crm.trip_cancellation`
  (pending or done) or is `cancelled`, a new invoice (from the schedule or typed), the issue of a draft, "Mark paid"
  on a schedule row, a draft proposal edit, a re-issued proposal link and a portal invite answer 409 `trip_cancelled`
  (`app.cancellation.ensure_not_cancelled`); what the cancel kept stays workable (a payment the client says they made
  is recorded on its invoice, refunds go on). The trip page shows its Proposal and Invoices cards read-only.
- **A cancelled trip is closed for planning too** (S262 round 3): the same 409 `trip_cancelled` for a new AA line, an
  AA hold / confirm / re-quote (a release stays), a new or confirmed agency line, new trip dates, a document shown to
  the traveller, any builder edit, a builder send and a proposal text; a trip whose cancellation is pending cannot be
  deleted (409 `cancellation_pending`). Messages stay open (the refund and the wrap-up; §2.7 stops automated sends
  only). The trip page's cards read one closed state (`web/components/pipeline/TripClosed.tsx`), which the cancel run
  sets at once.
- **Never cancelled early** (card R): with AA bookings the trip keeps its stage and a pending `crm.trip_cancellation`
  ("Cancellation pending" on the trip and the board; moves and Undo 409 `cancellation_pending`; the date rule, the
  commission sweep and the invoice reminders skip it). An invoice the cancel kept because it held money is voided once
  its refunds executed and nothing is left on it (`app.cancellation.void_if_refunded`). `crm_booking_sync` sends `POST /bookings/{id}/cancel` (`Idempotency-Key crm-cancel:<request
  id>`, the outage queue's backoff while Booking is down), cancels the booking's lines, writes the fee as a
  `cancellation_cost` money row of the line (the quote's FX lock, else the rate of Booking's `cancelled_at` day in the
  agency timezone; ledger total `cancellation_cost_base`), and once no
  AA booking is left moves the trip to `cancelled` (also after a `booking.cancelled` event). Booking `not_found` =
  nothing left to cancel; `invalid` counts so only when the feed already cancelled the booking's lines (it is also sent
  for a booking still live at AA). Another refusal, or a cancel that ran out of attempts (`gave_up`), opens the owner
  task `aa_cancel_failed` (closed when the cancellation completes), and Cancel again on the pending trip asks Booking
  again. A pending cancellation drops queued quote / hold / refresh requests (`trip_closed`) like a closed stage.
- **Web**: A-12 `/commissions` (`app/(crm)/commissions/page.tsx`: the kit DataTable in
  `components/commissions/CommissionsTable.tsx`, a card per commission on a phone; the kit underline tabs for the
  filter, `CommissionFilters.tsx`; `CommissionActions.tsx`, success in a page toast; assistant: "not available for
  your role"), the commission and the AA fee on the Bookings tab, the cancel dialog
  (`components/pipeline/StageDialogs.tsx`: fees, supplier bookings, refunds, commissions to keep, "Ask AA again"),
  the pending badge on the trip and the board, the supplier form field "Commission paid after (days)". Strings in
  `lib/i18nCommission.ts` (EN / VI), BFF lines card AA-957.
- **Not built (paid vendor)**: Stripe Checkout session expiry and Stripe refunds wait for the BYO Stripe provider.

## CRM finance: trip P&L, supplier bills, Bookings with AA, departure costs, profit cube (AA-1068)

S275 study items crm-25, crm-26, crm-30, crm-31, crm-32 (root `docs/research/2026-11-10-odoo-tour-travel-plugin-
comparison.md`, behaviour only, no plugin code); migration crm-051 (contract). Code: `app/finance/` (`pnl`, `bills`,
`aa_invoices`, `departures`, `agenda`), `app/routers/finance.py`, `app/booking/client.py` + `contract.py` (Booking 1.5
invoice reads), `app/ledger/lines.py` (the line's actual cost); web `components/finance/`, `app/(crm)/aa-bookings`,
`app/(crm)/departures/[ref]`; decisions in `docs/implementation-notes/AA-1068.md`.

- **Trip P&L** (`crm.v_trip_pnl`, security_invoker, one row per live trip, base currency now): revenue = issued
  invoices (open, payment_claimed, paid, paid_unfulfillable) **net of tax** by kind (sales = every kind but
  `service_fee` and `cancellation_fee`) minus refunds net of tax, at `crm.fx_resolve` of the issue / receipt day (no
  rate: left out, `revenue_unconverted`); a trip cancelled with a charge (`crm.cancellation_charge`) counts the charge
  net of tax at the cancel day's rate in place of its sales, their refunds and its fee invoices (`cancellation_charged`); costs = AA lines (the quote's
  frozen cost, or the AA settlement invoice of the booking once a balance / full line billed it, at the quote rate) +
  agency lines (`actual_cost` while a live supplier bill names the line, else planned) + AA `cancellation_cost` + the
  group departure share + the sales partner commission (crm-056: the trip's live `crm.partner_commission` rows, section "Sales
  partners" below); `commission_income_base` = the expected supplier commission of confirmed agency lines; margin,
  margin %, per traveller. Confirmed lines count; planning lines and money frozen in an earlier base currency are
  counted apart. The trip page's **Money** card (`GET /trips/{id}/pnl`) is owner / agent only.
- **Supplier bills** (`crm.supplier_bill` recorded -> paid | void, paid -> void; `crm.supplier_bill_line`): a bill of an
  own supplier pays confirmed agency lines of that supplier (CRM51 guard), FX of the bill date frozen on the bill; each
  billed line gets the money kind **`actual_cost`** (`crm.booking_line_actual_cost`: the sum of its live bill lines in
  one currency, at the latest bill's FX, `fx_basis bill_date`). Never edited: void and record again (voiding puts the
  planned cost back). The line view shows `actual_cost`, `cost_variance_base`, `actual_margin_base` only while billed.
  Routes `GET/POST /supplier-bills`, `GET/PATCH /supplier-bills/{id}` (owner / agent).
- **Bookings with AA** (`GET /aa-bookings`, owner / agent): Booking contract 1.5 `GET /invoices` (up to 3 pages),
  `/billing-status` and the detail of settlement invoices not read yet (at most 20 per open) are read live outside any
  transaction; `crm.aa_invoice_cache` keeps the summary and, per AA booking, the amount invoiced and whether billed in
  full (`booking_amounts`); lines, payments and the issuer text are never kept (`GET /aa-bookings/invoices/{number}`
  reads them live). The page shows the AA lines with their quoted and invoiced B2B price (a **mismatch** once an AA
  booking is billed in full at another amount than the sum of the quotes of its confirmed lines: several lines of one
  proposal share one AA booking, so the comparison and the P&L's AA cost are per booking), the invoices, and a **past-due banner** from Booking's credit status (holds paused while
  past_due), or from the copy's overdue invoices when Booking is down (`source = cache`).
- **Group departures** (`crm.departure_cost`, `crm.v_departure_trip`, `crm.trip.group_code`): a departure ref is an AA
  `departure_id` of a confirmed AA line or the agency's own group code (`PUT /trips/{id}/group-code`); only booked
  trips (booked, pre_departure, travelling, completed) are on a departure, so a lead with only a quote takes no share;
  a cost is `per_departure` (split by pax over those trips, cumulative rounding: the shares add up exactly) or
  `per_pax`, FX of its day frozen. `GET /departures/{ref}/pnl`, `POST/PATCH/DELETE /departure-costs` (owner / agent); the simple page
  `/departures/[ref]` until the departure roster card takes it over. A supplier named by a departure cost is not
  trashed (409 `supplier_in_use`).
- **Profit cube** (`GET /reports/profit?period=&owner=&dimension=`, owner / agent; CSV: `GET /reports/export?section=
  profit&dimension=`): booked trips (and cancelled ones) travelling in the window (start date, else the month), by
  `lead_source`, `sales_partner`, `product` (AA tour vs own services, from the confirmed lines' sell and cost),
  `travel_month` or `owner_member`. **Home "today" tiles** (`GET /reports/agenda`, every role, counts only): open
  proposals, deposits due this week, overdue schedule rows, trips leaving within 60 days with a traveller's passport
  number or expiry missing (presence only, nothing decrypted), overdue AA invoices (owner / agent), next group
  departures (AA's guaranteed status is null until the roster card stores the feed's `departure.guaranteed`).
- **Margin policy**: the four new tables carry a RESTRICTIVE `*_margin_roles` policy FOR ALL (owner / agent, or a job /
  seed actor), every money route asks `view_margin` / `money_fields_edit` first, and the money keys (`*_cost`, `*_b2b*`,
  `margin*`) are stripped by the serializer for any other role.

## Agency service catalogue and supplier booking requests (AA-1069)

S275 study items crm-23 and crm-24 (root `docs/research/2026-11-10-odoo-tour-travel-plugin-comparison.md`, behaviour
only, no plugin code); migrations crm-052 (expand) and crm-053 (contract). Code: `app/catalogue/service.py`,
`app/routers/catalogue.py`, `app/proposals/builder.py` (`add_catalogue_option`), `app/ledger/requests.py`,
`app/routers/ledger.py`, `app/mail/conversations.py` + `sender.py` (the supplier thread); web `/services`,
`components/catalogue/`, the builder drawer's "From catalogue" tab, the Bookings tab; decisions in
`docs/implementation-notes/AA-1069.md`. Defaults are the proposed ones (the business owner may adjust them).

- **Catalogue** (`GET/POST /services`, `GET/PATCH/DELETE /services/{id}`): every role reads it; `unit_net` and the
  tiers only for owner / agent (built only for `view_margin`, stripped by the serializer as a backstop); writes are
  `MONEY_FIELDS_EDIT` (owner / agent) and CRM52 in the database. Categories are the b6 ancillaries only (flight,
  insurance, visa, transfer, fee); a service names a live own supplier (never the AA row) or none (the agency's own
  fee). Delete is a hard delete: options added from a service are copies.
- **Add from catalogue** (`POST /proposals/{id}/blocks/{block}/catalogue-options`, `PROPOSAL_DRAFT`, every role: the
  prices are the owner's): the quantity follows the basis against the proposal's party (its room list, else the trip
  party: travellers, adults, rooms) and nights (typed, else the trip's `nights`, else the builder's days - 1), unless
  the member types one; the unit net is the tier of the party size. The option is an ordinary agency option (title
  "<name> x <qty>", supplier, sell, net), a **snapshot** with no key to the service; a service in another currency is
  converted at today's rate (409 `fx_rate_missing` without one); a switched-off service is 409 `service_inactive`, one
  not offered on the block's day 409 `service_not_valid`.
- **Supplier requests** (`POST /trips/{id}/supplier-requests`, `TRIP_EDIT`): the trip's planning (or requested) agency
  lines grouped per supplier; each supplier with an e-mail gets one draft (the member's earlier request draft for that
  supplier is replaced) in the trip's **supplier request thread** (`crm.conversation.agency_supplier_id`: no client,
  never the thread `app.mail.threads.conversation_for` picks for the client): travel dates, travellers by type, rooms
  (the latest builder version's room list), each service with its dates, and traveller display names when asked (by
  default for flight, insurance and visa suppliers). EN, or VI for an agency whose default locale is Vietnamese. A
  supplier without an e-mail gets no draft (`supplier_no_email`).
- **Send** (`POST /trips/{id}/supplier-requests/send`, `MESSAGE_SEND`): the normal send path (`conversations.send`: the
  member's mailbox else the agency's, 10 s Undo, held while disconnected); the dispatch row names the trip's lead (the
  gate drops it once that client is trashed or archived); the sender writes to the supplier's
  `contact_email_enc`, decrypted at send time (none -> `failed` / `no_supplier_address`); sender table row
  `supplier_request`. The lines it names become `requested` (`requested_at`, `request_ref` = the message id).
- **The answer**: the supplier's reply threads on the supplier thread by its headers (the normal forward-in); the trip's
  lines show `supplier_thread` (`replied` once the supplier wrote after the request). "Record confirmation"
  (`POST /lines/{id}/confirm` with `confirmation_no`) saves the number and runs `crm.booking_line_confirm` in one call
  (a requested line is confirmed like a planning one); "Back to planning" (`POST /lines/{id}/back-to-planning`) clears
  the request. No automatic number parsing.
- **Where `requested` counts**: pipeline value in the ledger totals (with planning), `waiting_on = supplier`, the
  cancel run's supplier checklist and its task closing (with confirmed), the trip P&L's "not counted yet" note. Only a
  planning line goes to the trash; a requested one is cancelled.

## Travel ops: departure roster, vouchers, server PDFs, rooming list, passport check, visa tracker (AA-1070)

S275 study items crm-22, crm-20, crm-21, crm-17, crm-15, crm-16 (root `docs/research/2026-11-10-odoo-tour-travel-plugin-
comparison.md`, behaviour only, no plugin code); migrations crm-054 (expand) and crm-055 (contract). Code:
`app/travel_ops/` (`passport`, `rooming`, `vouchers`, `visas`, `departures`), `app/routers/travel_ops.py`, the portal
wrappers in `app/portal/public.py` (routes in `app/routers/client_pages.py`), `app/documents/pdf.py` (the PDF writer),
`renders.py`, `pdf_job.py`; web `components/travelOps/`, `components/portal/PortalTravel.tsx`, `app/(crm)/departures`,
`app/(crm)/settings/travel`; decisions in `docs/implementation-notes/AA-1070.md`. The inclusion lists (crm-35) are
below; the guided "AA cancelled the departure" flow (crm-13) of the same issue is not built here.

- **Passport check** (computed, never stored): `expiry >= trip end + tenant_settings.passport_valid_months` (calendar
  months). Statuses `ok`, `expires_too_soon` (the "passport issue"), `missing`, `no_trip_end`; the API answers the
  status only, never the expiry. Shown on the trip party (`GET /trips/{id}` party `passport`, `GET /trips/{id}/
  passports`), the pipeline (`passport_issues` on booked / pre-departure / travelling cards, filter `?issue=passport`),
  Travelling now and the departure roster. The hourly workflow tick fires the trip event `passport.invalid_for_trip`
  once per entry into Booked (last 3 days); the AA library template `passport_invalid_for_trip` turns it into an urgent
  task. Settings > Travel (`GET/PUT /settings/travel`, owner changes).
- **Rooming list** (`crm.room_assignment`, `GET/PUT /trips/{id}/rooming`, the lead on the portal after the deposit:
  `PUT /crm/public/v1/traveller/rooming`): replaced whole; capacities single 1, double 2, twin 2, triple 3, quad 4,
  family 6; with an accepted proposal's `room_config`, no more rooms of a type than it has; a traveller entry without a
  `bed_pref` key keeps the one on file (the lead's portal save never wipes the agent's). The **ops release document
  is v2**: each traveller's `room` (number and codes only); the AA staff manifest can print a `room` column.
- **Vouchers** (`crm.booking_line_voucher`, `GET/PUT /lines/{id}/voucher`, `GET /trips/{id}/vouchers`): one per
  confirmed line; released to travellers (portal `GET /crm/public/v1/traveller/travel`, printable on its own with the
  `voucher_header`: agency, trip and dates, lead traveller, party size) when the
  trip is booked or later and the line is shown to travellers. An own supplier's contact name is opened for the voucher
  only; an AA line shows its short AA booking reference and departure (Booking's pick-up points are a Booking card).
- **Server PDFs** (`POST /trips/{id}/pdfs` 202 -> job `crm_pdf_render`, event kind, ids only): booking confirmation
  (booked trips), invoice, proposal (the e-mail text of the version), voucher; written by our own PDF writer (A4,
  Unicode via the embedded Liberation Sans, SIL OFL, `app/documents/fonts/`), stored as a sealed `crm.document`
  (uploader `system`, not shown to travellers until the agent shares it). No headless browser in the image. The
  header, footer and PDF author are the agency's display name (`app.documents.pdf_job.agency_brand`, the one place every
  render reads it): the CIS tenant profile has no colour or logo (CIS keeps the brand voice and `brand_name`; a colour
  exists only in the TripPlanner site theme, served to TripPlanner by a database view) and the CRM has no read of it,
  so branding beyond the name is a CIS contract change first (follow-up in `docs/implementation-notes/AA-1070.md`).
- **Visa tracker** (`crm.visa_template` owner-kept guidance, `crm.visa_application` + `crm.visa_checklist_item`):
  suggestions from the trip's destination countries vs each traveller's nationality (opened server-side), one
  application per traveller, country and booked trip, status moves `to_do -> collecting -> submitted -> approved |
  refused`, `not_needed`, `cancelled`; the granted number and validity sealed (`pii:government_id`), masked in lists;
  the traveller ticks their own checklist on the portal; one `visa_deadline_overdue` task per trip from the hourly tick.
  AA gives no visa advice (BYO).
- **Departure roster** (`GET /departures?scope=`, `GET /departures/{ref}/roster`, `POST /departures/{ref}/manifest`):
  one row per AA departure id of the agency's live AA lines, with the feed state (`departure.guaranteed` /
  `departure.cancelled` upsert `crm.aa_departure_state`; an older event changes nothing), trips, pax by type, Stage 3;
  the merged manifest for the agency's tour leader (picked fields from the ops release build + room + passport check,
  watermarked, never stored, one `report_export` audit event). It is a bulk reveal (card R, as the duty reveal): a
  reason from `BULK_REASONS` above 5 travellers or with a dietary / mobility column, the member's hourly reveal limit
  checked first (429 `reveal_rate_limited`), one `reveal` audit event per traveller (the reveal meter counts them).
  Every role; the `/departures/[ref]` page keeps the
  owner / agent group costs below the roster. AA departures are also a calendar layer (`aa_departure`).
- **Included / not included lists** (item crm-35, crm-059; `app/proposals/inclusions.py`, `app/routers/inclusions.py`,
  web `components/proposals/InclusionsPanel.tsx`, `components/settings/InclusionLibraryView.tsx`,
  `app/(crm)/settings/inclusions`): the agency library `crm.inclusion_item` (`GET/POST /inclusion-items`,
  `PATCH/DELETE /inclusion-items/{id}`; every role reads, owner / agent write: `money_fields_edit`; English text, optional
  Vietnamese, order, archive) and a builder version's lines `crm.proposal_inclusion` (`GET /proposals/{id}/inclusions`,
  `PUT /proposals/{id}/inclusions` with `seq`: the new list, whole; every role, drafts only). A pick copies the library
  text; a line already on the draft keeps its copy when the library line is archived or edited; a new pick of an
  archived line is 409 `inclusion_item_archived`. The AA tours' lists come from the CIS tour content in
  `crm.aa_catalog_tour.body` (`inclusions`, `exclusions[].text`), read-only: live on a draft, copied into the version at
  Publish (source `aa_tour`, every AA tour among its options) so the published version is frozen (CRM35); each page
  shows the tours of the chosen options (defaults in the builder and the texts, the client's picks on the client page).
  The lines are covered by the price hash (only when a version has some). One section, three forms: the client page
  (`inclusions`), the e-mail / WhatsApp text and so the proposal PDF ("Included" / "Not included" after the days). A
  working copy copies the agency lines (not the AA copy). Text proposals have no lists.
- **AA cancelled a departure** (item crm-13, crm-060 `crm.aa_cancel_case`, `app/travel_ops/aa_cancel.py`): a
  `departure.cancelled` the feed applies opens one case per live trip of the agency (not lost, cancelled, completed or
  being cancelled) with a live AA line or an AA option of a live builder version on the departure, plus one urgent
  owner task `aa_departure_cancelled` and the workflow trip event `departure.aa_cancelled` (library template
  `aa_departure_cancelled_notice`: an e-mail to the lead traveller from the agency mailbox, installed paused = opt-in;
  AA never messages travellers). Once per trip and departure: a replayed or repeated event opens nothing. The trip is
  flagged "AA cancelled" (pipeline filter `issue=aa_cancelled`, card badge, the trip page's guided card) while the
  case is open, or re-quoted onto a new trip and this trip not cancelled yet. The card offers:
  (a) **re-quote** (`POST /trips/{id}/aa-cancel/{case}/requote` {departure_id, start_date}, every role,
  `proposal_draft`): the same AA tour on another open departure the agent picks (`GET /aa/products/{source_id}`), as a
  NEW builder version prefilled from the trip's latest version with the tour (the accepted one first): the AA options
  move to the new departure, their quote is cleared (Refresh AA prices quotes again), day 1 moves so the tour's day
  falls on the new date; before booking the version is on the same trip (or its working copy); a trip with an accepted
  version cannot take a second one, so the re-quote opens a **new trip** in Proposal (same client, party, owner, the
  dates moved; stage reason `aa_rebook`) with version 1. Nothing goes to the client until the agent sends it.
  Idempotent: a re-quoted case answers what it made.
  (b) **cancel at no charge**: the trip cancel with reason `aa_cancelled` on a flagged trip forces the traveller charge
  to 0 (decision `waived`, reason `aa_departure_cancelled`, never the owner's call, the dialog's charge ignored), so
  each payment is prefilled for a full refund (`proposed` refund requests, §4.6 approval as usual); Booking's reason
  `departure_cancelled` costs the agency no fee. The run closes the case. Audit reasons are codes, never amounts.
  Dev E2E (agent-only, AA-1087 S278): `e2e/tests/aa-cancel.spec.ts` around the AA-Booking E2E write run, driven by
  the AA-Booking fixture seeder's output (`E2ECRMAACANCELFIXTURES`): `E2ECRMAACANCEL=prepare` puts the seeded tour as an
  AA item on the seeded departure in a draft builder version of a new trip; after Booking's E2E staff address cancels
  that departure, `verify` waits for the one active case and the `issue=aa_cancelled` board filter. Skipped in
  `e2e-dev.yml` (no phase set).
- **AA tour discontinued** (AA-1093 part 1, S278 audit gap M29; crm-063 `crm.tour_retired_flag`,
  `app/catalog/retired.py`): no new CIS feed (root plan D9). The hourly `crm_catalog_sync` already stores a CIS 410 as
  `served_state = 'retired'`; the store's state write sees the transition (served or paused -> retired, retired ->
  served; paused alone is ignored, root ADR 0015 decision 5) and queues one `crm_tour_retired_fanout` row in the same
  transaction (payload: the tour's uuid source id, `purpose`, `changed_at` in epoch microseconds; a source id that is
  not a uuid cannot ride an ids-only payload and is counted `fanout_unkeyed`). Per agency: one flag per item of a live
  trip (an AA option in a draft / published / sent builder version, a held / quoted / booked AA line; dead lines
  left out), one task `aa_tour_discontinued` per trip with a new flag (root plan D10: the trip owner while an active
  member, else the agency's first active owner; normal priority when only booked lines are hit, else urgent). The CRM
  never changes a booking line, an option or a proposal for it (Booking keeps every hold and booking). Reinstated:
  the flags clear, a trip left without an open flag and with the open task gets an internal note in the agency's
  language; the agent closes the task. The trip page card (`aa_tour_retired` of `GET /trips/{id}`), the card badge
  and the pipeline filter `issue=aa_tour_retired` read the open flags; the builder option badge reads the live
  `no_longer_offered`.

## Sales partners and their commission (AA-1071)

S275 study items crm-28 and crm-29 (root `docs/research/2026-11-10-odoo-tour-travel-plugin-comparison.md`, behaviour
only); migration crm-056 (contract). Code: `app/partners/` (`service`, `public`), `app/routers/partners.py`,
`app/routers/partner_pages.py`, `app.tenancy.partner_link_tx`; web `app/(crm)/partners`, `components/partners/`,
`app/c/r/[token]`, `app/c/a`; decisions in `docs/implementation-notes/AA-1071.md`.

- **Partners** (`crm.sales_partner`): sub-agent, referrer, freelancer or online agency; name and contact under the
  vault DEK; commission terms (percent / fixed per traveller / net rate, rate, basis revenue or margin, payout days).
  The owner manages them (`sales_partner_manage`, DB guard CRM54); every member lists the names (the trip picker);
  owner and agent see the terms and the open totals. A partner a trip or a mail names is deactivated, not deleted.
- **The trip's partner** (`PUT /trips/{id}/sales-partner`, every member; the intake link sets it on a new trip with
  lead source `sales_partner`). A booked trip (booked, pre_departure, travelling, completed) with a partner accrues
  one `crm.partner_commission` through the database trigger, whatever path booked it; leaving the booked stages,
  the trash or another partner cancels an accrued row; approved and paid rows stay. The trip cancel cancels the open
  ones unless the member keeps them (`keep_partner_commission`). The commission is a cost in `crm.v_trip_pnl`
  (`partner_commission_base`) and the profit cube's `sales_partner` dimension.
- **Commissions** (`GET /partner-commissions`, owner / agent; `POST .../approve|unapprove|pay|cancel`, owner only:
  `partner_commission_approve`); approve recalculates first; `POST /trips/{id}/partner-commission/sync` refreshes an
  accrued row; `GET /sales-partners/{id}/statement` is the partner's CSV (trip reference, never a name).
- **Partner links** (`POST /sales-partners/{id}/links`, owner): the intake link `/c/r/<token>` (an agency inquiry
  form stamped with the partner) and the partner page `/c/a/<token>` (signed link + e-mail code from the agency's
  mailbox through the dispatcher: `conversation.sales_partner_id`, `dispatch_queue.sales_partner_id`, codes on
  `crm.sales_partner_otp`, purged after 24 h by `crm_stage_rules`). Route class row above.

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
- **Reserved `.invalid` recipients** (AA-1014, every environment): an address whose domain is or ends in `.invalid` (RFC 6761; case, whitespace, root dot and IDNA folded, `app/mail/recipients.py`) is never handed to a provider or to AA's SES: a client message ends `failed` / `invalid_tld` (dispatch row `dropped` / `send_failed`), the Test button sends no test mail, `SesMailer` logs the skip; the action that queued the mail (proposal sent, portal invite, code issued, dev echo) is unchanged. A unit test fails when a module of `app/mail` calls a provider without `deliverable()`.
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

## CRM-28 Module 9 SMS and WhatsApp (AA-958)

Design v3 §6.3 CRM-28 (M9, §5.4, WF-11), lane `data-privacy-integrations.md` §2.8 group G / §6.4, **as changed by the
BYO errata S247** §1 / §2: the agency's **own Twilio account** only (no AA account, no AA subaccount, no Tech Provider;
AA pays nothing). Migration crm-036; code in `app/messaging/`, routes in `app/routers/messaging.py` and two public
webhook routes in `app/routers/public.py`; decisions in `docs/implementation-notes/AA-958.md`. No Infra change: the
webhook origin is `MESSAGING_HOOK_BASE`, else the first `INTAKE_PAGE_ORIGINS` entry (the client host, whose ALB rule
already sends `/crm/public/*` to the API).

- **Providers** (`app/messaging/providers.py`): one `TextProvider` interface (`send`, `check`, `templates`).
  `TwilioProvider` (Messages API for SMS and WhatsApp, Content API for templates, the account read for health; stdlib
  HTTP through the mail `HttpClient`) and `SandboxProvider`, a deterministic stub with no network, accepted on `dev` /
  `test` / `local` only, so the Dev live check costs $0 (a recipient ending `0000` is refused, `0001` is transient, an
  Auth Token starting `revoked` is revoked; three fixed templates, two approved).
- **Connection** (Settings > Channels, owner, `INTEGRATIONS_MANAGE`; every other role sees status and channels only):
  Account SID + Auth Token (+ optional API key), the SMS sender (E.164, alphanumeric id or Messaging Service SID) and
  the WhatsApp sender. The account is read before anything is stored; the credential is `crm.integration_credential`
  provider `twilio`, agency level, sealed under the `integration` DEK like the AA-945 SMTP login (`Vault.decrypt_secret`
  is lint-allowed in `app/messaging/sender.py` only). A reconnect keeps the hook id, so the URLs pasted into Twilio stay
  valid. Disconnect deletes the credential and, by cascade, the connection and templates. Test button: account read
  now + an optional test message to the owner's own phone (SMS: a short text; WhatsApp: an **approved template**, the
  chosen one or the first service one, values `Test`; none approved -> 409 `template_required`); the incoming webhook
  is checked by a reply (`inbound_check` `pending` until a message arrives after the test, then `ok`). Health states
  `connected | needs_attention | disconnected`.
- **Send**: `POST /crm/v1/messaging/messages` (`MESSAGE_SEND`) stores an outbound `crm.message` (channel `sms` /
  `whatsapp`, text under the mail DEK) and its `crm.message_delivery` row, then queues one `crm_dispatch` row with the
  10 s Undo (Undo / Edit are the AA-945 message routes). WhatsApp free text only inside the 24-hour window opened by the
  client's last inbound message, else **409 `template_required`** (card R); outside it only an `approved` template with
  its variables. A template whose category is not `UTILITY` / `AUTHENTICATION` is **marketing**: 409
  `no_marketing_consent` without the client's current marketing yes, queued with `purpose marketing` so the
  dispatcher's consent gate (`no_consent`) applies at send time too; the composer hides it from such a client. The
  dispatcher (`ChannelSender` routes e-mail to the AA-945 sender, text to `TextSender`) re-checks
  the window, the template, the client's number and the opt-out at send time; a refused token disconnects the
  connection with one owners' alert (`messaging_disconnected`, AA SES); nothing is ever sent another way.
- **Webhooks**: `POST /crm/public/v1/hooks/twilio/{hook_id}` (inbound) and `.../{hook_id}/status`, form-encoded,
  64 KiB. Not under the per-address public limit (Twilio posts every agency's callbacks from one shared address pool,
  and a 429 loses an inbound message for good): only unknown hook ids (404) are counted per address
  (`HOOK_MISSES_PER_MINUTE` 120; a hook id this task already resolved skips that check), and an address over that is
  429 before any work. No `X-Twilio-Signature`: 403 before the database. Each task caches a resolved hook's Auth Token
  (`app.messaging.webhook.HookCache`, 15 min): a bad signature is 403 without the database and never locks the hook
  out; a good one is checked again against the database. `X-Twilio-Signature` is checked with the agency's own Auth Token over the URL the CRM
  printed (never the Host header) and the `AccountSid` must match, before anything is stored: **403
  `invalid_signature`**. Inbound: deduplicated by `MessageSid`; threaded by the sender's phone blind index (one client
  -> their latest conversation on the channel or their latest open trip, else a conversation of the client with no
  trip, listed in the Inbox under "Clients with no open trip" (`GET /conversations?no_trip=true`, link to one of
  the client's trips); none or two -> Unmatched); media not kept;
  WhatsApp window = now + 24 h; `STOP` / `START` (and `OptOutType`) set / clear `opted_out_at` on the client's
  conversations of that channel. Status: applied only when its rank is higher (card E: out-of-order callbacks keep the
  latest state); error 21610 marks the conversation opted out. Answer: an empty TwiML `<Response>`.
- **Events** (`app/messaging/events.py`, the seam CRM-29a workflows and CRM-29b tags build on): `message.received` (a
  bell item for the trip owner) and `message.sent` as `crm.metric_event` rows on the trip, ids and codes only, then the
  in-process listeners registered with `subscribe()` (AA-959: the workflow triggers, `app.workflows.triggers.on_message`).
- **Web**: Settings > SMS and WhatsApp (`web/app/(crm)/settings/channels`, `components/messaging/ChannelsSettings.tsx`:
  status, who pays, the two webhook URLs, connect / reconnect, Test, templates + Refresh, Disconnect), the trip's
  WhatsApp / SMS panel (`components/messaging/TextMessagesPanel.tsx`: the thread with delivery states, the window, free
  text or an approved template with its values and preview, Undo, why a channel cannot send), the Unmatched inbox
  shows the channel, and the Inbox lists known clients' text conversations with no open trip
  (`components/messaging/NoTripInbox.tsx`).
- **Dev live check**: `api/scripts/messaging_hook_probe.py` posts a signed Twilio-shaped inbound message or status
  callback to an agency's hook URL (`PROBE_AUTH_TOKEN` = the sandbox connection's token; `--bad-signature` expects 403).

## CRM-29a Module 10 workflows and tasks (AA-959)

Design v3 §6.3 CRM-29a (M10, §5 automation, R2, R9, R20, R44 step 1, Fix B8 Undo, Fix C3 "cancel stops runs"), A-09
Tasks / A-17 Automations, WF-10, BYO errata S247 §1 (nudges to travellers go from the agency's mailbox) and §3 (the
digest is AA system mail). Migration crm-038; code in `app/workflows/`, routes in `app/routers/workflows.py`; decisions
in `docs/implementation-notes/AA-959.md`. CRM-29b (AA-960: tags, system tags, coverage, ready to travel) builds on it.

- **Rules** (`crm.workflow_rule`, `app/workflows/model.py`): a trigger, conditions and 1..20 steps. Triggers:
  `stage_enter` (a stage), `event` (`proposal.viewed` / `option_chosen` / `change_requested` / `accepted`,
  `invoice.payment_claimed` / `overdue` / `unfulfillable`, `message.received` / `message.sent`), `date_anchor`
  (departure, return, stage entered, last activity, created + offset days), `special_date` (a client's birthday,
  passport expiry, trip anniversary + offset; the run is about the client) and `manual` (a task list a member applies,
  R9). Conditions: stages, trip styles, lead sources, destinations, children, AA services, Stage 3 incomplete,
  `tags_any` (CRM-29b, below). Steps: `create_task` (a library kind or a
  custom title; due = anchor + offset, 09:00 local for a date; assignee = the owner of the trip / client or a member),
  `send_email` (an agency message template merged with `{{traveller_first_name}}` ...; transactional or marketing; a
  birthday or trip-anniversary rule's messages are always marketing, 422 `marketing_only` on transactional),
  `send_text` (an approved SMS / WhatsApp template of the agency's Twilio, variables from merge fields; never free
  text; never in a rule started by `message.sent`, 422 `message_loop`), `set_field` (`waiting_on`, the trip owner),
  `wait` (days / hours, or until a trip date). Message loops: a text a workflow sent (dispatch `origin workflow`)
  starts no rule, and a message rule starts at most one run per trip per 12 hours (`triggers.MESSAGE_COOLDOWN_HOURS`). The owner edits
  (`WORKFLOW_EDIT`: create, install from the library, edit with `version`, active / paused / archived; a rule with a
  send step needs its template before it is switched on, 409 `templates_missing`); owner and agent apply
  (`WORKFLOW_APPLY`: a task list to trips with a dry run, stop a run); everyone reads.
- **AA library** (`app/workflows/library.py`, code with key + version, worded by the web `wf.tpl.<key>`): arrival flight
  T-48 h (R44), Stage 3 nudge T-21, pre-departure checklist, quiet client (3 days), reply to a client's text, review after
  the trip, birthday greeting, passport expiring (200 days), trip anniversary, visa and documents task list. One live
  rule per template; a template that sends starts paused until its message template is picked.
- **Runs** (`crm.workflow_run`): one per rule and **trigger key** (`stage:<history id>`, `event:<event or message id>`,
  `date:<trip id>:<anchor>:<offset>:<date>`, `special:<code>:<client id>:<date>` (the anniversary
  `special:trip_anniversary:<trip id>:<date>`), `manual:<request id>:<trip>`; every key names its subject, so two trips
  or clients on one date each get their run; UNIQUE, so a tick or a signal that runs twice starts nothing twice, card
  E). Started inside the change that triggers them
  (`app/workflows/triggers.py`: `app.stages.move` and the date rule and a new inquiry, `app.proposals.events.record`,
  the AA-958 message listener; each start in a savepoint, a failure never fails the change); a member's move or apply
  starts the run after the Undo window (20 s, Fix B8) and Undo of the move stops it (`undone`). The trip cancel's hook
  (`app.cancellation.CANCEL_HOOKS`, registered by `triggers.install()` in `app.main`) stops every active run of the
  trip (`trip_cancelled`, counted in the dry run's `workflows_stopped`). The engine (`app/workflows/engine.py`) stops a
  run itself when the rule is archived, the trip is lost (unless the rule starts on Lost), trashed, cancelled or
  cancelling, or the client is trashed or archived (`archived`); a paused rule's runs wait. Each step is logged as a
  code (`created`, `already_open`, `queued`, `skipped_no_consent`, ...); a database error fails the run once
  (`step_failed`). Marketing sends without the client's current marketing yes are **not sent**, logged
  `skipped_no_consent` and audited `workflow_send` denied (card R); the dispatcher's gate checks again at send time.
  Workflow mail leaves from the trip owner's mailbox, else the agency's; a missing / disconnected mailbox holds it
  (`held_reconnect`); dispatch rows are `origin workflow` (no Undo delay).
- **Tasks** (`app/workflows/tasks.py`): `GET /tasks` buckets Overdue / Today / Next 7 days / Later / Waiting for dates
  in the agency's timezone (scope mine / team / trip / client, or done in 7 days; one `list_view`); a custom task
  (kind `custom_<hex>`: a member's own, or a workflow step's with the suffix fixed per rule and step) carries its title
  under the vault DEK; a task names a trip or a client, never both. Due dates anchored on departure / return follow the
  trip's dates (the hourly tick and every date change, `tasks.redate`) unless a member set the date by hand
  (`due_overridden`, R20). `GET /tasks/{id}` of `confirm_arrival_flight` adds the trip's arrival flights with a public
  status link (flight numbers opened and audited). The source (`workflow`, `manual`, `aa_event`, `system`) is derived.
- **Digest** (`app/workflows/digest.py`, `crm_task_digest`): the owner's switch `tenant_settings.task_digest` (default
  off); at 07:00 local each active member with open tasks gets the counts (overdue, today, 7 days, waiting) through
  AA's SES (`task_digest`), no client, trip or task text.
- **Web**: `/tasks` (`components/workflows/TasksBoard.tsx`: buckets, sources, Done / Reopen, reassign in the team view,
  New task, the T-48 h flight links, the digest switch for the owner), `/automations`
  (`components/workflows/AutomationsBoard.tsx` + `RuleEditor.tsx`: rules with trigger and steps in words, preview count,
  switch on / pause / archive, the editor, the library), the trip page's Automations panel
  (`components/workflows/TripWorkflows.tsx`: runs with each step's outcome, stop, Check then Apply a task list).

## CRM-29b tags, system tags, coverage, ready to travel again (AA-960)

Design v3 §6.3 CRM-29b (R3 tags + tag workflows, R29 system-tag workflows, R30 missing-workflow badge, R36 ready to
travel again; card R "a workflow on an archived contact is suppressed", card E "bulk apply previews the count; the R36
list explains its reasons"). Migration crm-061 (expand); code in `app/tags/` and `app/workflows/coverage.py`, routes in
`app/routers/tags.py` and `GET /workflows/coverage`; decisions in `docs/implementation-notes/AA-960.md`.

- **Tags** (`crm.tag`, `crm.tag_link`): kind `agency` (the agency's words) | `system` (computed, codes `sys_*`, guard
  CRM55: only a `job:` / `platform:` / `seed:` actor writes them); `applies_to` trip | client; a fixed `code` made once
  from the first name (`app.tags.model.code_for`: ASCII `a-z0-9_`, never `sys_`), so renaming keeps rules working; tone
  = a kit badge tone; an archived agency tag keeps its links and is not offered. A link names exactly one trip or
  client (its tag's subject), source member | system (+ `reasons`). Never trashed: removing deletes; every key
  cascades (tag, trip, traveller); hold rule 4h on links (CRM10 -> 409 `legal_hold`).
- **Who** (`app/permissions.py`): `tag_apply` every role (tag one record, a new tag from the picker), `tag_bulk_apply`
  owner + agent, `tag_manage` owner (rename, recolour, archive, delete), `marketing_view` owner + agent.
- **Bulk** (`POST /tags/{id}/bulk`): add / remove on explicit ids or a filter (trips: stage / owner / tag; clients:
  archived / owner / tag), at most 500. The dry run is the preview: `count`, `already`, `held` (remove skips held
  records), `archived_suppressed` (archived clients tagged without automations), a sample; the apply sends the
  previewed `expected` count back, another count is 409 `count_changed`.
- **Tag workflows**: trigger `event` + `trip_tag.added` (about the trip; other trip conditions apply) or
  `client_tag.added` (about the client, like a special date), always with `conditions.tags_any` (422 `tags_required`;
  codes must be the agency's tags of that subject or a known system code, 422 `tag_not_found`). A new link starts
  each matching active rule once (`app.workflows.triggers.on_tag_added`, key `tag:<link id>`); on an archived client
  nothing starts and each rule's start is audited `workflow_start` denied `client_archived` (card R); the engine also
  stops a client run once the client is archived (CRM-29a). `tags_any` is also a plain trip condition of any rule.
- **System tags** (`app/tags/system.py`, step 6 of the hourly `crm_workflow_tick`, in a savepoint): R29 trip tags from
  the AA catalogue cache (`crm.aa_catalog_tour`, served tours only) of the trip's live AA lines and AA options: day /
  multi-day, the CIS activity vocabulary (8 codes), easy / challenging; reasons = the tours. R36 `sys_ready_to_travel`
  on a client at the special-date hours: a completed trip with an end date, no open trip, home >= 9 months; reasons =
  months home, repeat traveller, anniversary within 60 days, new live AA tours in a country they travelled to. New
  links start the tag workflows; stale links are deleted unless held.
- **Coverage** (R30, `GET /workflows/coverage`, computed on read): per active `stage_enter` / `trip_tag.added` rule, the
  live trips in that state that match its conditions and never had a run of it, plus `by_trip`. [Run] = the existing
  apply (dry run first).
- **R36 list** (`GET /marketing/ready-to-travel`, owner / agent): live, not archived clients with the system tag, the
  reasons, the last completed trip, the marketing consent state (`yes` / `no` / `unknown`); names opened and the view
  audited (`list_view`, `ready_to_travel`). The agency's own marketing; AA never reads it.
- **Lists**: the client directory filters by a client tag (`?tag=<code>`) and returns each client's tags; the pipeline
  board cards carry their trip's tags.
- **Web**: `components/tags/TagBar.tsx` (trip page and client page: agency tags, system tags with reasons, add / new
  tag), `BulkTagButton.tsx` (pipeline column header, clients directory: Preview then Apply), `TagsSettings.tsx`
  (`/settings/tags`), `CoveragePanel.tsx` (`/automations`, under the pipeline board; cards get a "Missing automation"
  badge), `/marketing` (Ready to travel again; campaigns arrive with CRM-36), the automation editor's tag trigger and
  tags condition. Words in `lib/i18nTags.ts` (system tags by code `tag.sys.<code>`, reasons `tag.reason.<code>`).

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
  `/c/p/[token]` (`components/proposals/ProposalPage.tsx`) and `/c/t` (AA-953; `components/portal/PortalPage.tsx`; the
  session cookie never reaches page script; only the `client.*` strings ship, `lib/clientMessages.ts`), BFF lines card
  AA-946 in `lib/bff.ts`. Dev E2E: `e2e/tests/lifecycle-thin.spec.ts` (portal steps need `PORTAL_OTP_DEV_ECHO`).
- **Portal per person (AA-953, CRM-24a; `app/portal/person.py`, `docs/implementation-notes/AA-953.md`)**:
  - **One link, cookie exchange** (§2.9 rule 1): `/c/t/<token>` (web route) sets `aa_portal_link` (Path
    `/crm/public/v1/traveller`) and redirects to `/c/t`; the page talks only to `/crm/public/v1/traveller/*`. The
    token routes `/crm/public/v1/portal/{token}/*` stay (same functions).
  - **Who reaches whom (Fix E4)**: an e-mail address belongs to one client of the agency, so a companion without an
    address of their own is reached through the lead's (`delivery_of`). A code goes to that address; when it reaches
    more than one traveller with portal access, `POST /session` answers `pick` and the same code with a `traveler_id`
    opens a session acting as that person only (the `otp_challenge` row is re-bound to the chosen access). Two people
    on one address get two sessions; `POST /switch` moves a session inside its group without a new code. **A session
    is bound to the address its code went to** (`otp_challenge.sent_to_bidx`, crm-029) and re-checked on every call:
    it acts as a traveller only while that address still reaches them, so a companion given their own address, or a
    lead whose address the agency changed, ends every session the old address proved, on any link.
  - **Per person** (R22, §2.9 rules 2-3): own Stage 3, flights (`crm.traveler_flight`, local time + zone at the
    airport, R33), a write-only passport scan (`document` kind `passport_scan`, `uploaded_by_kind = traveler`, never
    readable here: 403 `document_write_only`, card R), the documents the agency shared with this traveller or the
    whole trip (another traveller's: 403 `document_not_yours`), the trip's lines shown to travellers without prices
    or cancelled ones, the accepted proposal's days. The lead also sees the group with names and each person's way in
    and gives a companion without an address one at the invite (`POST /companions`: only for a companion on this trip
    alone (409 `ask_your_agent`: the address lands on the agency-wide client record), never over an address on file,
    409 `email_in_use` for another client's address without naming them; refusals audited as denied and capped at
    three a day per lead (429 `too_many_attempts`). The lead gets a link back only for a companion reached through
    the lead's own address; a companion with their own address gets it at that address, and one with a live link is
    409 `already_invited` (the lead never rotates another adult's link).
  - A **lost or cancelled** trip has no portal (404 `link_not_found`).

## CRM-24b offline PWA (AA-954)

Design v3 C-09 "Itinerary (installable, offline)", R16, §2.9 rule 7 (Fix A2), WF-8; decisions in
`docs/implementation-notes/AA-954.md`. No migration.

- **Installable** (client host only, `/c/*`): `/c/portal.webmanifest` (`lib/portalManifest.ts`; `id`, `start_url` and
  `scope` `/c/t`, generic name "My trip" / "Chuyến đi của tôi" by `?lang=vi`, the app's own mark as PNG icons under
  `web/public/c/icons/`, drawn by `tools/pwa/pwa_icons.py`), the iOS meta and apple-touch icon on `/c/t`, and the
  "Install trip app" button (`beforeinstallprompt`) or the iPhone "Share, then Add to Home Screen" hint.
- **Service worker** `/c/sw.js` (`lib/portalServiceWorker.ts`, scope `/c/t`): keeps the **page shell only** (the
  server-rendered `/c/t`, which holds UI strings and the date locale, and the `/_next/static/*` files that page loaded,
  sent by the page once a traveller is signed in and replaced as a whole); network first for `/c/t`, cache first for
  static files. It never answers or stores an API call, a document, the link landing `/c/t/<token>` or another origin.
- **Itinerary-only copy** (`lib/offlineItinerary.ts`): the page keeps one Cache Storage entry
  (`aa-portal-data-v1`, `/c/t/offline-itinerary.json`) built by **copying named fields** of the signed-in home: the
  trip title and dates, the days (date, title, place, zone), the services without class or price, the agency name and
  `agency_contact`, `saved_at`, `link_expires_at`, `offline_ref`. Never Stage 3, the envelope, documents, flights,
  names, the group or payments (card R: `web/tests/offlinePwa.test.ts` inspects every cache).
- **Rules** (server wins, nothing is written back): every signed-in answer replaces the copy (also after "Switch");
  a signed-out answer keeps it only when its `offline_ref` is this link's; 404 (link gone) clears; sign-out and
  "Remove from this device" clear every `aa-portal-*` cache; a copy past `link_expires_at` is never shown and is
  dropped. No network: the page shows the copy with "last updated" (card E); the `online` event reloads the home. All
  writes need a connection ("You are offline"); there is no background sync and no push (v1).
- **API** (`app/portal/public.py` `home`): both answers carry `offline_ref` (a hash of the token hash under its own
  prefix: opaque, stable per link); a signed-in answer adds `link_expires_at` (the link's expiry, `ClientLink.expires_at`)
  and `agency_contact` (`{"email"}` of the agency mailbox the portal mails come from: the trip owner's, else the
  agency's; `null` without one). `api/tests/integration/test_portal_offline_api.py`.
- Dev E2E: `e2e/tests/lifecycle-thin.spec.ts` step 6b (offline reload, sign-out clears the caches).

## CRM-34 reports + AA insights (AA-966)

Spec modules 16 and 17, design v3 A-14 Reports, A-01 Home tiles, S-03, R23 (Lost analytics), R28 (year-on-year
tiles), report catalog S258 CRM-01 / CRM-02 (CRM-05 monthly mail and CRM-03 / 04 / 06 stay with their cards).
Implementation notes: `docs/implementation-notes/AA-966.md`. Every agency number is computed on page open from the
agency's own rows (`app/reports/`), so `crm.metric_daily` (design R28, 25 months) is not created yet. One expand
migration, `crm-031` (the trip-level insight fact for module 17).

- **Routes** (`app/routers/reports.py`, AGENCY, `Action.REPORTS_VIEW`, every role): `GET /crm/v1/reports/summary?
  period=30d|90d|12m|ytd&owner=me|<member id>`, `GET /crm/v1/reports/tiles` (Home: last 12 months, everyone),
  `GET /crm/v1/reports/export?section=…` (CSV). Read-only SQL in `app/reports/queries.py` inside `tenant_tx()`, so FORCE
  RLS keeps each agency to its rows; trashed rows never count.
- **Definitions.** Window = local days of `tenant_settings.timezone`. Cohort = trips created in the window; won = the
  trip reached Booked (its stage, or a stage-history row a later Undo did not reverse; a booked-then-cancelled trip
  stays won); the funnel counts, per forward stage, the cohort trips that reached it. Lost / cancelled reasons: trips
  that ended so in the window. Money (owner / agent only, `view_margin`): confirmed booking lines by confirmation date,
  head money rows in the base currency (frozen at confirmation, R17 / R18), margin = sell - cost over lines with both
  (as the ledger). The assistant gets no money section and no money query runs (crm-014 would return no rows anyway);
  its Adventure Asia share is counted in booking lines. Outstanding follows A-10 Receivables: open invoices in the
  `RECEIVABLE` statuses (open, payment_claimed, expired), paid = payments only (a refund never reopens an invoice),
  each invoice's balance clamped at 0 before the currency sum; plus schedule rows still due that no live invoice bills
  yet ("not invoiced yet": a billed row's money is in its invoice, never counted twice); each in its own currency. Team: per member open trips, new, won, overdue tasks, first
  reply (median minutes from the trip's creation to the first message sent from the CRM; imported mail never counts).
- **Comparison (R28).** Same window a year earlier when the agency's first trip is on or before that window's start;
  else the last three calendar months (`trend_3m`); `none` without trips.
- **Success metrics.** One source query per measurable spec §6 success line (`SUCCESS` in `app/reports/service.py`);
  modules 8, 13 and 14 answer `not_built` until their cards land.
- **CSV** (`app/reports/export.py`): UTF-8 BOM, CRLF, the spreadsheet formula guard on text cells (numbers stay
  numbers), a money section is 403 `role_forbidden` for the assistant (the margin serializer filters JSON only), and
  every download writes a `report_export` audit event (the module 16 success metric).
- **AA insights** (crm.ops.v1 1.5, `GET /crm/ops/v1/insights?dimension=&quarter=YYYY-Qn`, any OPS reader): one
  calendar quarter only (422 `invalid_quarter` otherwise), so two answers never overlap or nest and cannot be
  subtracted. `console.platform_insights` opens `platform_tx()` per active agency (synthetic agencies only outside
  prod), reads the opt-out as crm_app first, then the facts as `crm_insights` (`tenancy.as_insights_reader`): the
  destination dimension from `crm.v_insight_fact` (crm-006, one row per trip and destination), every other dimension
  from `crm.v_insight_trip_fact` (crm-031, trips per band combination, so a trip's party counts once);
  `app/reports/insights.py` checks the grain, publishes a cell only with >= 10 travellers from >= 3 agencies and no
  agency above 70 % of them, returns no agency id and no total, only how many cells were held back. Not guaranteed:
  the cells are live, so changing one agency's opt-out between two reads of a quarter shows that agency's share
  (platform write, audited; a frozen per-quarter table is the follow-up). Like `/data-coverage`, no per-agency audit event (totals only). The
  CIS admin page S-03 reading it is a follow-up in AA-CIS-App.
- **Web**: `/reports` (`web/app/(crm)/reports/page.tsx`, kit `KpiTile` + `DataTable`, filters in the URL, the CSV
  through the BFF line `reports/(summary|tiles|export)`), the Home tiles (`components/reports/HomeTiles.tsx`; a failed
  read shows nothing), the access log names a report download.
## CRM-31 onboarding + import (AA-962)

Design v3 §2.7 "Onboarding (Fix E2)", §11.5, R6, R43 (in-app); errata S247 §1 / §2 (Connections, controller settings,
the owner acknowledgement). Implementation notes: `docs/implementation-notes/AA-962.md`.

- **Checklist** (`app/onboarding.py`, `GET /crm/v1/onboarding`, every role): three required steps (connect a mailbox,
  invite the team, embed the inquiry form) and optional ones (import clients, review duplicates, card payments or bank
  transfer, deposit rules, templates, privacy settings, kickoff call, help centre). Done = the agency's data shows it
  (a connected `channel_account`, a second member or an invitation, an inquiry from a form, an imported client, ...) or,
  for the steps the data cannot show, the owner's mark (`PUT /onboarding/steps/{key}` `done` / `later` / `open`,
  `ONBOARDING_MANAGE`, owner). A required step never goes on "Later". Home shows the required steps and the open
  optional ones; `/onboarding` the full list.
- **Connections** (same read): mailbox, card payments (Stripe; bank transfer as a flag), SMS / WhatsApp (Twilio), the
  ICS calendar feed and an e-sign provider when one exists, each in `not_set_up` / `connected` / `needs_attention` /
  `disconnected`, read from `channel_account`, `payment_provider_account`, `integration_credential`, `calendar_feed`:
  status codes only, never a credential, account reference or address. Web: Settings > Connections and privacy.
- **Controller settings and acknowledgement** (`PUT /onboarding/controller`, `POST /onboarding/acknowledgement`,
  `DATA_SETTINGS`, owner): the privacy-notice link (https), the data-request contact (one line), the agency's consent
  wording; the version of the shared-responsibility text (`ACK_VERSION`, the text is in the web EN / VI; a proposal
  until the business owner's legal wording replaces it, then a new version asks again), who and when, one
  `acknowledge` audit event. 409 `version_changed` for another version. The public inquiry form reads them
  (`app/intake/public.py` `read_form`): the agency's privacy link when the form has none of its own, its consent
  wording on the contact box (none = the page's default text) and its data-request contact under the form.
- **Import** (`app/imports/`, `CLIENT_IMPORT`, owner; web Settings > Import clients): four files in the design's order,
  clients (`client_ref`, `name`, optional contact fields, DOB, nationality, channel, marketing consent),
  relationships (two client refs and a relation kind), trips (`trip_ref`, the lead's `client_ref`, `title`, dates,
  status `completed` / `cancelled` / `lost` / `inquiry`, companions), notes (`text` and a client or trip ref). CSV
  (comma, semicolon or tab; UTF-8) or the first sheet of an `.xlsx` (`formats.py`, a standard-library streaming
  reader: `iterparse`, an 8 MB inflated-byte budget per zip member, capped shared strings; run in a thread), at most
  5 MB and 5,000 rows. `POST /client-imports/preview?entity=` (raw body) is the dry run: **a missing
  column is 422 `missing_columns` before anything is read or written (card R)**; otherwise the counts per outcome
  (`create`, `match`, `already_imported`, `duplicate_in_file`, `invalid`), the `possible_duplicate` warnings and up to
  200 row issues (row numbers and codes, never a value) and the file's SHA-256. `POST /client-imports?entity=&sha256=`
  starts the job with the same file (409 `file_changed` otherwise); `GET /client-imports[/{id}]` reads the progress
  from the job rows (no import table: a run leaves no personal data behind). Dedupe: the row's ref
  (`import_ref_bidx` or one of the client's `import_ref_alias_bidx`, **re-running an import creates nothing, card E**),
  then the e-mail / phone blind index (a known client is matched and gets the ref, as its own or as an alias; nothing
  else changes), then the earlier rows of the file (a row sharing an earlier row's e-mail / phone is a `match` with
  code `duplicate_contact`: its ref points to that client). Every ref the clients phase accepted resolves in the
  relationships, trips and notes phases. Imported clients get
  `lead_source = 'import'` and belong to the importing owner; a marketing consent column writes one `consent` row
  (`source = 'import'`) for a new client only. Trips: one `trip_stage_history` row (reason `import`), both party twins,
  age bands at departure. `phone_region` (ISO alpha-2) reads local numbers without `+`.
- **Duplicates and merge** (`app/travellers/merge.py`): `GET /client-duplicates` (every role, one `list_view` event)
  lists live clients sharing the normalised name, e-mail or phone (blind indexes, no decrypt to find them);
  `POST /travelers/{keep_id}/merge {other_traveler_id}` (`CLIENT_MERGE`, owner) moves trips (both twins; the lead role
  when both were on a trip), notes, documents, conversations, dispatch rows, call slots and public-form submissions,
  re-makes relationships from the kept client, copies consents (time and source kept), moves health codes when the kept
  client has none, fills the F1 fields the kept client lacks (re-sealed under its row id), carries every import ref
  of the other client (own ref when the kept one has none, else aliases: a re-run finds the kept client), then puts
  the other client in the trash (`app/lifecycle/trash.py`: its purge erases the rest, one F1 record stays). 409
  `merge_blocked` (with `blockers` and `swap_possible`) when the client that would go has client links, portal access,
  signatures or accepted terms (that evidence names it); 409 `legal_hold` for a held client.
- **Help** (`web/app/(crm)/help/page.tsx`): the guides each step and connection links to (`/help#<topic>`), EN / VI.

## CRM-30 duty of care + calendar (AA-961)

Design v3 R37 (A-02 Travelling now, F9 "an AA incident shows a banner") and R15 (calendar; the ICS link is the
design's fallback, §5.10). Implementation notes: `docs/implementation-notes/AA-961.md`.

- **Incident flags.** `crm_booking_sync` applies the booking.b2b.v1 1.1 events `departure.incident` /
  `departure.incident_cleared` (`app/booking/sync.py` `apply_incident`): one `crm.incident_flag` per departure while
  open (a replay or a second incident while open: `incident_already_open`), one urgent `aa_incident` task per affected
  booked / pre-departure / travelling trip (the agent decides whom to reach; AA never messages travellers), the clear
  closes the flag (`incident_not_open` when none is open; an event without a departure or a known code:
  `incident_incomplete`). The affected trips are read at view time (live trips with a booking line on the departure):
  an agency without one sees nothing, a line added later is covered.
- **Travelling now** (`app/duty/service.py`, `/crm/v1/duty/*`, action `duty_of_care_view`, every role): booked,
  pre-departure and travelling trips overlapping a 1-31 day window from today in the agency's timezone (no end date =
  open-ended, the trip stays travelling until its return is set; a trip the stage job completed counts through its
  return day, up to today), grouped by region (the running AA line's tour country from the catalog cache, else the
  trip's first destination), the day number and the accepted proposal's day place, the running lines; travellers **once each** with all their trips
  (card E) and only which contact fields exist, **no names**. `GET /duty/reach?region=` is one region on one day.
  `GET /trips/{id}/incidents` feeds the trip banner (open, and cleared in the last 14 days).
- **Bulk reveal** (`POST /duty/reveal`, action `reveal_contact`, or `reveal_sensitive` with the emergency contact):
  name, phone, e-mail, emergency contact of at most 50 travellers; up to 5 the reason is `duty_of_care`, above 5 one of
  `BULK_REASONS` is required (422 `reason_required`, card R); the member's reveals of the last hour (the reveal meter,
  per process) plus this request may not pass 60 (429 `reveal_rate_limited` with `Retry-After`, one denied event); one
  `reveal` audit event per traveller. Health codes stay one by one on the client page (consent checked there).
- **Calendar** (`app/calendar/events.py`, `/crm/v1/calendar`, action `calendar_view`): departures and returns of
  booked / pre-departure / travelling / completed trips (a completed trip keeps its events: a subscribed calendar keeps
  their UIDs), `due` payment schedule rows, `open` / `payment_claimed` invoices, signing deadlines (start - 21 days while a signature is
  pending), open tasks, requested / confirmed calls; scope `mine` (own trips, own or unassigned tasks on own trips,
  calls on own trips or clients) or `agency`; at most 400 days and 2,000 events.
- **ICS feed** (`app/calendar/feeds.py`, `/crm/v1/calendar/feed[s]`, action `calendar_feed_manage`): a new link revokes
  the member's active one; the URL `<PARTNER_WEB_ORIGIN>/crm/public/v1/calendar/<token>.ics` is returned once; stop =
  revoked (404 from then on; the page shows the revoked banner, saying whether an owner did it); the owner lists the
  team's active links. The feed (`app/calendar/ics.py`) carries a fixed English label, the trip's short reference and a
  link to the trip, never a title, name or amount; 30 days back to 365 ahead; counters written at most every 10
  minutes. No Google / Microsoft calendar write (bring-your-own sync is a later card).
- Web: `/travelling` (regions, incidents, "Reach travellers here" with the kit DataTable selection and the reveal
  controls), `/calendar` (month agenda, kind filter, my link, the team's links for an owner), the trip workspace
  banner; BFF lines card AA-961.

## CRM-26 Module 8 ops release (AA-956)

Design v3 M8, F8.3 "Ops release" (Fix B2), F10.3, C12 step 6, S-02 (Fix B3), R33, R44 data; data standards DM-6.8 and
the §7 rows `ops_release` / `supplier_manifest_export` (trip end + 90 days, purge with a receipt). Migration crm-034;
code in `app/ops_release/` (`build.py`, `service.py`, `job.py`, `staff.py`). Implementation notes:
`docs/implementation-notes/AA-956.md`.

- **Release when an AA line is confirmed.** `crm_ops_release_sync` (the only creator, guard CRM45) gives a trip that is
  not Lost / cancelled / trashed and has at least one confirmed AA line one live `crm.ops_release`. The Booking sync
  queues a one-trip nudge on a confirm / release answer and on `line.confirmed` / `line.declined` / `booking.cancelled`,
  so a confirm reaches AA ops within about a minute; the hourly pass catches the rest (Stage 3 filled in, a flight
  added, dates moved).
- **The document** (`build.py`, sealed under the `ops_release` DEK, canonical JSON): per traveller the name, the age
  band on the departure date, dietary / mobility codes (only while the latest `health_data` consent is yes), arrival /
  departure flight (number, airport, local time, IANA zone: R33), the waiver's insurance choice; per trip the dates,
  party size, the agency's name and time zone, the confirmed AA lines (title, tour, Booking departure / booking ids,
  service dates). Never phone, e-mail, emergency contact (X6 default), passport, nationality, date of birth, notes,
  messages, documents, money or the trip title. A changed text is sealed again and `payload_version` moves up.
- **Completeness** (counts only): Stage 3 details on file, waiver signed (when the agency has one), both flights.
  **T-21**: still incomplete within 21 days of departure -> one urgent `ops_release_incomplete` task for the trip owner
  (anchor `ops_handoff`), `t21_alerted_at`, and the log line `crm_ops_release_incomplete_t21` (ids and counts) plus the
  ops check `ops_release_incomplete_t21` that mails AA ops (S263); once per release.
- **Purge date** = `end_date` + 90 days, recomputed every pass (dates moved -> purge moved); at `purge_at` the job
  deletes the document and the manifest records and keeps `deletion_receipt` (counts) and `ops_release_purge` in the
  agency's audit. A held trip is skipped (CRM10). A trip past its window is never released again.
- **Withdraw** (C12 step 6): the trip cancel (`app.stages.cancel`, dry-run effect `ops_release_withdrawn`) and
  `POST /crm/v1/trips/{id}/ops-release/withdraw` (owner / agent, action `trip_cancel`) mark the live release withdrawn
  (AA ops lose it at once) and nudge the job, which purges it now if the trip has not started. No confirmed AA line
  left -> the job withdraws it (`aa_lines_cancelled`; a new confirm releases again). A cancel or the agency's own
  withdrawal is never lifted. Each withdrawal (the cancel, the agency's own, the job's) logs `crm_ops_release_withdrawn`
  (ops check `ops_release_withdrawn`: the notice to AA ops) and stays on the S-02 board for 14 days.
- **Alarm lines after the commit**: the withdrawn and T-21 lines are `service.Notice`s the code queues and the route
  (cancel, withdraw) or the job writes only once its transaction committed (the break-glass pattern), so a rollback
  never alarms and a T-21 alert whose `t21_alerted_at` was not saved is not repeated. `Notice.emit` raises the ops
  check too (`service.OPS_ALERT_CHECKS`).
- **One trip at a time**: the job syncs each trip and purges each release in its own savepoint
  (`service.item_tx`, which also drops that item's queued audit events, except denials, and the DEKs it generated): a
  failing trip is counted `failed` and logged `ops_release_trip_failed` by id, the agency's other work commits, the
  next hourly pass tries it again (a failed one-trip nudge is not retried).
- **Agency side**: `GET /crm/v1/trips/{id}/ops-release` (every role; the Activity lines for owner / agent) feeds the
  trip page panel "AA ground operations" (`web/components/pipeline/OpsReleasePanel.tsx`): status, completeness, views,
  the purge countdown or the receipt, "Withdraw from AA". Never the document. The access log names the `aa_ops` lines.
- **AA ops (S-02)**: `/crm/staff/v1`, `app/routers/staff_ops.py`, group `crm-ops` only (`crm-support` 403), every route a
  GET, each opens `ops_tx()` (`app.actor` = `ops:<staff sub>`, audit actor kind `aa_ops`; agency active or locked, else
  404): `GET /ops-releases?days=` (the board over every agency, no personal data, one `ops_release_list` event per
  agency), `GET /agencies/{tenant}/trips/{trip}/ops-release` (the document; 404 `release_not_found` unless live and the
  trip still has a confirmed AA line now; `view_count` + 1 and one `ops_release_view` event: "AA ops viewed trip X"),
  `GET .../ops-release/manifest?fields=` (CSV text of `name, age_band, dietary, mobility, arrival_flight,
  departure_flight`, default the first four, the watermark on every row, formula-safe cells; one
  `crm.supplier_manifest_export` record and one `ops_manifest_export` event; the file is never stored). **Fix B3**: no
  incident flag here; the board and the release carry `incident_admin_url` = `<BOOKING_ADMIN_URL>/b2b-desk`.
- **Staff web screen (AA-1019)**: `/staff/ops` (board, window 14 / 30 / 60 / 120 days, the Booking incident admin link)
  and `/staff/ops/<agency>/<trip>` (one release: every render is one counted view; the manifest download with column
  picks), section "AA staff console". Not built: paid paging (SMS / WhatsApp) to AA ops.
- Reports: the Module 8 success metric `ops_release_errors` (AA departures in the window that left with an incomplete
  or missing release, a withdrawn one excluded) is live.

## CRM-25 contracts and waivers (AA-955)

Design v3 §6.3 CRM-25 (M12, R34, R13 decline; §5.7 e-sign, O-6, §3.6 retention Fix B16); migration crm-025; code in
`app/esign/` (`merge.py`, `envelope.py`, `seal.py`, `signing.py`, `terms.py`, `events.py`, `service.py`), the portal
signing in `app/portal/public.py`, the terms re-accept in `app/proposals/public.py`, routes in `app/routers/esign.py`
(`/crm/v1`) and `app/routers/client_pages.py` (`/crm/public/v1`); decisions in `docs/implementation-notes/AA-955.md`.

- **Versions** (Settings > Agreements): booking terms, waiver and insurance declination; a text may hold the merge
  fields of `app/esign/merge.py` (agency name, trip title / dates, traveller role, signer name / role, signing date;
  never a traveller F1 field), any other `{{name}}` is 422 `unknown_merge_field`. Hashes pin the version text; the
  rendered text's hash is in the evidence.
- **R34 terms acceptance**: `crm.terms_acceptance` rows at the proposal accept (`proposal`, with the price hash), at a
  re-accept (`reaccept`) and inside each signing envelope (`envelope`); client address + user agent under the vault
  DEK, their SHA-256 in the evidence record. **Re-accept rule** (card E): a trip whose accepted proposal's deposit is
  unpaid while a published booking terms version has no acceptance on the trip: the proposal page shows
  `terms_reaccept` (`POST /crm/public/v1/proposal/terms`, `/p/{token}/terms`), the deposit "Mark paid" and Record
  payment are 409 `terms_reaccept_required`, and the client's deposit pay box reads `terms` (no bank details, no "I have
  paid"; the claim and the card session are 409 `terms_reaccept_required` with nothing written). **Pinned terms**
  (S261): with published booking terms, a builder skeleton starts with them and a draft without terms is pinned to
  them at builder publish / text send (`proposals.service.pin_current_terms`, before the price hash), so the client
  accepts them with the proposal. A version that names terms keeps them; once that version is retired, a builder draft
  is not publishable (blocking check `terms_retired`, or `terms_not_published`; the view's `terms_status`) and the
  builder / Proposal tab show the named version marked with "Use the current terms"; settings take only the published
  version, and a working copy of a version on retired terms starts on the current ones (S262 round 3).
- **One envelope per traveller** (C-08): booking terms + waiver + the insurance declination when the traveller
  declines (the choice is asked whenever a declination version is published); each document's id and SHA-256 as
  shown (409 `document_changed` with the current documents); typed name, consent tick, a fresh signing code.
  **O-6**: an adult signs their own; the adult lead signs as guardian for each minor (under 18 on the start date, from
  the F1 DOB, read only to compare; one `guardian_check` audit event); a minor's own access is 403 `guardian_signs`,
  anyone else 403 `not_guardian`. The rendered envelope + audit page (canonical UTF-8 text) is sealed: `kms:Sign`
  RSASSA-PSS SHA-256 with `KMS_SEAL_KEY_ID` (`alias/crm-<env>/seal`), the HMAC dev stub without it on dev / test /
  local, 503 `esign_not_configured` on stg / prod. The envelope is stored on the `signature_event` row (kind `signed`,
  docs DEK); the agency opens it as a reveal with a reason (`POST /crm/v1/signatures/{id}/envelope`, `verified`).
  **Not PAdES yet**: no PDF; the seal is detached over the text envelope (follow-up in the implementation notes).
- **Card R**: every refused signing code (expired, wrong, missing, attempts used up) is a `signature_event`
  `code_refused` row plus a denied audit event; every signing code sent is `code_sent`.
- **Gate**: unchanged rule (s3 counts the waiver signed per traveller, whoever signed it), so a guardian's signature
  of a minor counts for the minor.
- **Retention (DM-7 claim-period exception)**: `signature_request` (the typed name) leaves with its trip / traveller
  (a guardian's erase takes the minors' signatures they signed); `terms_acceptance` + `signature_event` stay until
  `keep_until`, then the daily lifecycle purge deletes them.
- **Web**: the portal envelope (`components/portal/SignEnvelope.tsx`, the v0 test ids kept), the proposal page's
  re-accept (`components/proposals/TermsReaccept.tsx`), the trip's Signed documents panel
  (`components/proposals/TripAgreementsPanel.tsx`), BFF lines card AA-955.
- **The e-sign v0 limit stays** (the signing code goes out from the agency mailbox, whose Sent copy its owner can
  read); the evidence records `code_delivery` as before.

## CRM-19 B2B client + AA lines (AA-947)

Design v3 §6.3 CRM-19 (M3 supply, M6 AA lines, R17, R18, R23; checklist: M2M client, catalog cache, lines from quote /
hold / feed only, "from"-price warning, AA-aware Lost reasons, outage queue); migration crm-017; code in `app/booking/`
(`client.py`, `contract.py`, `aa_lines.py`, `sync.py`, `views.py`, `products.py`, `checker.py`) and `app/catalog/`
(`client.py`, `store.py`, `sync.py`), routes in `app/routers/aa.py`; decisions in `docs/implementation-notes/AA-947.md`.

- **M2M client**: `booking.b2b.v1` 1.1 (1.2 `GET /holds/{id}`, 1.4 `GET /tariff`) and `catalog.http.v1` 1.2 over the aa-m2m pool with one `TokenProvider` for the
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
- **Suggested retail price** (AA-1030, crm-046; root ADR 0019 b15): Booking's quote already carries the agency's tier
  or contract rate card (the `b2b_price`). The builder suggests a sell for every AA option (`aa.suggested_sell`,
  `aa.suggested_basis`, `app/proposals/retail.py`): the owner's markup for the product type
  (`tenant_settings.retail_markup_pct`, Settings > Payments, `GET` / `PUT /crm/v1/settings/retail-markup`, owner only)
  on the B2B price, rounded up to the cent; else, for a tour, AA's RRP ("from" price) x party; else none. A markup-based
  suggestion is derived from the B2B price, so only owner / agent see it. An owner or agent who adds an AA option
  without a sell gets the suggestion as the sell (an assistant's option still waits at 0); "Use" on the option sets
  it later. The below-"from" warning applies to it like any sell.
- **Retail pricing** (AA-1066, crm-048; `app/proposals/pricing.py`, `app/proposals/discount.py`; decisions in
  `docs/implementation-notes/AA-1066.md`):
  - *Price per traveller type*: `PUT /crm/v1/proposals/{id}/options/{option}/prices` (`MONEY_FIELDS_EDIT`: owner /
    agent) replaces an option's rows (`adult`, `child`, `infant`, `single_supp`), `[]` removes them, `use_suggested`
    takes the grid the page shows; the option's sell becomes their sum (an agency item's net the sum of its unit nets
    when every row has one; an AA item never takes a unit net: 403 `aa_net_from_quote`); a typed sell or net is 409
    `amount_from_prices` while rows exist. The builder view carries `prices` (unit net for owner / agent only),
    `suggested_prices` (the suggestion, else the current sell, shared per adult and child head; infants and the
    supplement at 0) and `pricing.party` (the party by the agency's age bands, Settings > Pricing; AA's own quote keeps
    infants under 2). The client page lists the types and counts (`price_lines`), never a unit price. The price hash
    covers the rows (and the discount) only when a version has them, so older hashes are unchanged.
  - *Retail price rules*: the owner's `crm.retail_price_rule` rows (Settings > Pricing, `GET` / `PUT /settings/pricing`,
    `POST /settings/price-rules`, `PATCH` / `DELETE /settings/price-rules/{id}`, `PAYMENT_SETTINGS`, at most 50) change
    the AA-1030 suggestion of an AA option: every matching `add` rule sums, of the matching `best_only` rules only the
    first by priority applies, never below -90 %, rounded up to the cent; the service date is the option's day. The
    rules used go to `option.price_rules` when the sell comes from the suggestion (an owner / agent adding an AA option
    without a sell, `PATCH .../options/{id}` `use_suggested`, or the suggested grid); a typed sell clears them.
    Agency items get no rule (they have no base price to adjust).
  - *Discount*: `PATCH /proposals/{id}/builder` `discount_pct` (a money field) comes off the charged total (never the
    pay-on-arrival items); every total, the deposit, the client page and the accept follow it (`builder._charged`).
    Above `tenant_settings.discount_owner_limit_pct`, or with the discounted total below the net, a non-owner's
    Publish is 409 `discount_needs_approval` (check `discount_needs_approval`) until an approved
    `crm.discount_approval` of the trip covers that percent (and the loss): `POST /proposals/{id}/discount-approval`
    (owner / agent) asks (an urgent `discount_approval` task for an active owner), `POST
    /proposals/{id}/discount-approval/{approval}` (`DISCOUNT_APPROVE`, owner only; the DB guard CRM48 too) decides. A
    raised discount is no longer covered (the approval re-opens); an owner's Publish records their own approval.
    The AA ledger lines an accepted discounted version writes carry each option's share of the discount
    (`builder.line_sells`), so the line sells add up to the accepted total.
- **Retail payments** (AA-1067, crm-049 / crm-050; `app/payments/schedule_plan.py`, `app/payments/schedule_settings.py`,
  `app/cancel_policy/`; decisions in `docs/implementation-notes/AA-1067.md`):
  - *Instalments*: a builder version carries `proposal.schedule_plan` (deposit first, at accept; 0..10 instalments,
    each a share and a date; at most one balance, last, the rest); `deposit_pct` / `balance_due_on` mirror its deposit
    and last line. A new version starts from `tenant_settings.schedule_template` (Settings > Payments, `GET` / `PUT
    /settings/payment-schedule`, owner; NULL = the AA default 30 % now, 40 % at T-60, the rest at T-30), dated from day
    1 (a line keeps `days_before` while it follows the template). `PATCH /proposals/{id}/builder` `schedule` (lines,
    `null` = the deposit / balance pair), `schedule_reset` (a money field each). Publish blocks on
    `schedule_dates_required` / `_order` / `schedule_after_start` / `schedule_amount_zero`; the price hash and the seal
    (CRM31) cover the plan. Amounts round half up per currency (whole units for VND, JPY, ...), the deposit at least one
    unit, the last line the rest: the accept writes one `crm.payment_schedule` row per line (`kind` deposit |
    instalment | balance, `seq`, `percent`) adding up to the accepted total. Text proposals keep the pair. The stored
    status stays due | paid | void; `crm.v_schedule_status` derives `live_status` (partial / overdue) and the money on
    each row (trip overview, client page, portal). An instalment is invoiced, paid, voided and cancelled like a balance;
    the stage gate keys on the deposit row only.
  - *Reminders to travellers*: workflow `date_anchor` values `schedule_due_soon` (offset -60..0) and `schedule_overdue`
    (1..60) start one run per open row of the accepted proposal (`schedule:<row>:<anchor>:<offset>:<due date>`), never
    for a row paid in full, at most one per row every 3 days; the engine skips a row paid meanwhile (`skipped_paid`)
    and fills `{{payment_amount}}` / `{{payment_due_date}}`. Library templates `payment_due_soon` (T-7) and
    `payment_overdue` (+3) install **paused** (opt-in; `starter_mail: true` copies an EN / VI starter message into the
    agency's templates); mail goes from the trip owner's mailbox, else the agency's.
  - *Cancellation policy*: `crm.cancellation_policy` versions + `crm.cancellation_policy_tier` ("N days or more before
    departure: X %, at least M"), owner-managed (`/cancellation-policies`, every role reads; CRM49: a published
    version never changes, a new version retires the old one and takes its default flag). A version going out (builder
    Publish, text Send) pins the agency default unless the agent chose one (`proposal.cancellation_policy_id`, sealed,
    in the price hash); the client page shows it and booking terms merge it (`{{cancellation.policy}}`, also on the
    portal signing pages).
  - *Charge and refund*: the cancel dry run's `charge` block = days before departure, the frozen policy (else the
    default, `policy_not_frozen`), tier, charge, paid, kept, refund due, fee invoice; `charge: {mode policy | override
    | waive, amount, reason}` on `POST /trips/{id}/cancel`; an agent lowering the policy charge past
    `refund_owner_threshold_base` is 403 `cancel_charge_owner_only`; `charge_below_aa_fee` warns when the charge is
    below Booking's dry-run fees. The run prefills each refund request with what is left of the payment once the charge
    is kept from the oldest payments, issues a `cancellation_fee` invoice for the charge not covered by money kept, and
    records `crm.cancellation_charge` (`GET /trips/{id}/cancellation-charge`; the trip P&L seam of crm-30: fee =
    `charge_amount`).
- **Book now and online payment** (AA-1072, crm-057, items crm-02 / crm-03 of the S275 study; `app/intake/booking.py`,
  `app/intake/booking_settings.py`, `app/payments/gateway.py`, `app/payments/vnpay.py`; decisions in
  `docs/implementation-notes/AA-1072.md`):
  - *Book now*: the public AA tour page books for the agency whose **inquiry form key** its link carries
    (`/trip/<tourId>?f=<key>`), so `/crm/public/v1/book/{key}` opens `intake_tx` like the inquiry form (unknown,
    disabled, locked = 404 `form_not_found`; the form's honeypot, page origins and default owner apply). The owner's
    switch `tenant_settings.public_booking_enabled` (Settings > Payments "Online booking", `GET` / `PUT
    /settings/online-booking`, `PAYMENT_SETTINGS`) is **off by default** (404 `booking_not_offered`). The AA tour is
    the link's `p` catalogue key, else the page id when it is a catalogue key (a tour without versions); CIS adding
    `source_id` to `GET /v1/trip/{id}` is a follow-up the web already reads. Only fixed departures from today + 1,
    with seats, of a product Booking sells the agency (404 `tour_not_bookable` otherwise: the page shows "Ask the
    agency"). `POST .../price`: a Booking quote (no hold, nothing stored) and the agency's retail on it, the builder's
    own suggestion (owner markup on the B2B price, else AA's RRP, then the price rules), shared per head and rounded
    **up** to the currency's unit (whole dong): the per-traveller grid; no suggestion = 409 `no_retail_price`; a
    retail under the quote's B2B price (no markup, AA's RRP per head under the B2B price of the party, e.g. one
    traveller's single supplement) = 409 `price_on_request` at the price check and at the booking (never sold below
    cost: the public path has no owner to pass the builder's discount gate). The price check has its own per-address
    bucket (`RATE_LIMIT_BOOKING_PRICE_PER_MINUTE`, 20), not the intake one the booking, hold, accept and "Pay online"
    share (S277 live check). AA-1092 (crm-062): and a ceiling per agency for every address together, before Booking is
    asked: the agency's `tenant_settings.public_price_per_minute` (default 60, 10..600, the owner sets it in "Online
    booking", `PUT /settings/online-booking` `{price_checks_per_minute}`; audited by field), 429 `agency_rate_limited`
    with `Retry-After` (the page says it is busy). Counted in each API task's memory (no shared limiter store), so N
    tasks give an agency up to N times its value. The widget (`GET /book/{key}`) is bounded the same way: its
    departures come from a 30 s cache per (agency, tour, date window) (`ProductCache.departures`, at most 2048 answers,
    errors never kept), and a read that does reach Booking takes one of the agency's reads a minute (the same value,
    its own `widget:<tenant id>` bucket), else 429 `agency_rate_limited`; the price check and the booking still read
    departures live. `POST
    .../bookings`: origin, honeypot (202, nothing written), consent, answers, at most 5 attempts an hour per address
    and agency (`ip_hash`; a `price_on_request` refusal does not count), one `crm.public_booking_attempt` (a repeated press = `already_created` /
    `booking_in_progress`); the departure and the quote with no transaction held; then one transaction: **always a new
    client** (the typed e-mail is not verified and the answer is a live client link, so an existing client with the
    same e-mail or phone is never reused or unarchived; the trip gets an `internal` note to confirm and merge them on
    Clients > Duplicates, audit `public_booking_known_contact`), a trip at **Proposal** (`lead_source` and
    stage reason `web_booking`, the party, the departure's dates, the country's destination), a builder v1 (day 0, day
    1 with one "Tour" block holding the AA option at the retail sell, its traveller rows, the agency's schedule
    template), published and sent by the builder's own `publish` / `send` (price hash, terms, cancellation policy),
    the attempt `created`; the answer is `/c/p/<token>` (201). The client page's payment dates follow its schedule:
    before the accept a plan date already past reads as today, after it the summary's balance date is the schedule
    rows' (the accept dates a past line on its own day). A Booking refusal marks the attempt
    `refused` with the code (409); another failure rolls back and marks it `failed`. The client holds, accepts and pays
    on the existing client page; **a web booking's deposit invoice is issued at the accept** (actor the client link).
    A booking also takes one of the agency's hourly ceiling (AA-1092: `RATE_LIMIT_INTAKE_FORM_PER_HOUR` per form key,
    its own bucket `book:<key>`, charged after the origin, honeypot and answers): 429 `form_rate_limited`.
  - *Online payment (BYO VNPay)*: Settings > Payments "Online payment (VNPay)" (`GET` / `PUT
    /settings/payment-methods/vn-gateway`, owner): the merchant code on the agency's `payment_provider_account`
    (`external_account_id`, `charges_enabled`, VND) and the hash secret + mode in its `vn_gateway`
    `integration_credential` (sealed; never answered back); modes `sandbox`, `live`, `stub` (dev / test / local only, 422
    `mode_not_allowed` elsewhere). Connected = an **active** `vn_gateway` account with its credential: a disconnect
    (Settings > Payments) keeps the credential for the webhooks of sessions already started, but the status reads
    "Not connected" and the client page offers no "Pay online"; a reconnect reactivates the agency's last
    `vn_gateway` account row (never a second row). The client page offers "Pay online" (`pay_online`) on a payable
    invoice in the gateway's currency; the card button creates a `crm.payment_session` for the invoice's **open amount computed on the
    server** (15 minutes) and answers the signed gateway URL; the return page `/c/p/paid` drops the gateway's query and
    opens `/c/p?payment=returned` (the box reads itself again once). The IPN (`GET /crm/public/v1/hooks/vnpay/ipn`,
    `payment_session_tx`; AA-1092: a notice without a `vnp_SecureHash` of the SHA-512 hex shape is `97` before any
    lookup and spends its address's misses, as `01` / `99` do; a well-formed but wrong signature spends
    `IPN_REF_REFUSALS_PER_MINUTE` of the pair (client address, merchant reference), then 429 for that address on that
    reference only: never the address alone, which VNPay shares, and never the reference alone, so a forger cannot keep
    VNPay's genuine notice out)
    checks the HMAC-SHA512 signature with the agency's own secret first (`97`), then the merchant
    code, the amount (`04`), the session state (`02`); a failure closes the session `failed`; a success goes through
    the same code as "Record payment" (method `online`, provider `vn_gateway`, the gateway's number as reference,
    once per reference: `payment_provider_ref_uq`): the invoice paid, the row settled (Booked, portal, Booking confirm)
    or the re-hold of a late deposit. **Never blocked by a business error**: money the invoice cannot take (paid or
    void meanwhile, above what is left, newer terms, late money of a failed session) is still recorded on the invoice
    without a schedule row, the session goes to `review` and the trip owner gets an urgent `payment_review` task; the
    gateway is answered `00`. The stub (`/crm/public/v1/pay/stub`) checks the signed URL like VNPay, shows Pay /
    Cancel, and sends a VNPay-shaped IPN signed with the agency's own secret through the same handler; it never
    simulates a sandbox or live account. Refunds of an online payment are made in the agency's VNPay portal and recorded
    as today (the gateway's refund API is a follow-up).
- **AA prices** (AA-1030, `booking.b2b.v1` 1.4 `GET /tariff`; `app/booking/tariff.py`, web `/aa-tariff`): the
  agency's own AA tariff, AA's B2B price per tour, season (departure window) and pax band with its tier or contract
  rate card discount and AA's RRP per person, read live from Booking. `GET /crm/v1/aa/tariff?currency=&format=json|csv
  &source_id=` needs `aa_tariff_view` (owner, agent; the assistant 403 `role_forbidden`): the margin serializer does
  not cover the CSV bytes, so the action is the guard. JSON renames Booking's `price` / `price_per_person` to
  `b2b_price` / `b2b_price_per_person` so the serializer would strip them too (defence in depth); `format=csv` passes
  Booking's own file on (its `tariff.CSV_COLUMNS`, its file name when it is a plain `.csv` name), never a second CRM
  file shape. One `aa_tariff_view` audit event per read (currency, format, tour asked; no amount);
  `Cache-Control: no-store`. Errors: `unknown_partner` / `forbidden` -> 409 `aa_partner_not_active`; `invalid` -> 422
  `aa_tariff_invalid`; another refusal -> 422 `aa_tariff_refused`; Booking down, throttled or a contract break -> 503
  `booking_unavailable` + `Retry-After`. Both tariff calls are bulk reads (8 MiB body cap, 25 s; every other Booking
  call 2 MiB / 10 s), and the route reaches Booking only through `TariffReader`: at most 2 tariff reads in flight per
  API task and 1 per agency (else 429 `aa_tariff_busy`, `Retry-After: 5`, Booking not asked), the same ask in flight
  joined, a finished answer kept 60 s as its bytes (24 MiB in all; the browser still gets `no-store`). `/me` answers
  `permissions.aa_tariff_view`, which hides the page's links
  (Settings, the builder's AA drawer) from the assistant. The page: table, currency select, Download CSV (BFF route
  marked `download`: the API's file name passes on), Print (print CSS); prices are indicative, a quote is binding.
  S270 (AA-1029): the select offers USD plus the quote currencies AA has an approved FX rate for today:
  `GET /crm/v1/aa/tariff/currencies` (same `aa_tariff_view` gate, no audit event: no price; a server-only web route)
  answers `{currencies, rate_date, checked}` from Booking `GET /fx-rates` (`BookingClient.fx_rates`,
  `contract.fx_rates`), one answer per API process for 10 min (`TariffCurrencies`; FX rows are global); Booking down,
  refusing or not connected -> every quote currency with `checked: false`, never a 5xx. A `?currency=` not offered
  shows USD with a `role=status` notice; Booking's 422 `invalid` for a currency without a rate (the tariff read)
  is `aa_tariff_invalid` -> "AA does not price in this currency today. Pick another currency." The BFF passes the
  API's `Retry-After` on for a 429 or 503 only when it is 1 to 4 digits (`lib/retryAfter.ts`; `lib/proxyResponse.ts`
  still forwards Content-Type only), and the download's busy message names those seconds.
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
  confirm). One row is settled through one path: the schedule's own "Mark paid" and the row's void are refused while
  an issued invoice bills the row (409 `invoice_issued_for_row`; a draft invoice is voided with the row), and a
  schedule invoice takes no payment once its row is not due (409 `schedule_not_due`). An invoice whose payments were
  all refunded can be voided (409 `invoice_has_payments` while it holds money net of refunds, `refund_pending` while
  a request is open), which frees the row for a new invoice. Every invoice view carries `payments`, `refund_requests`, `paid_amount`,
  `refunded_amount`, `balance_amount` (total - payments; a refund never reopens an invoice).
- **Refunds** only from an approved `crm.refund_request`: proposed (owner / agent, never more than is left to ask back
  on the payment: 422 `amount_above_refundable`) -> approved / rejected (`REFUND_APPROVE`: the owner any amount, an
  agent up to `tenant_settings.refund_owner_threshold_base` on the invoice's approved refunds plus this one, converted
  to the base currency with the ledger's FX resolution (a split refund still reaches the owner; a rejection is judged
  on the request alone); no rate = only the owner; 403 `above_threshold`; an agent never approves a request they
  made, 403 `refund_self_approval`) -> executed (`PAYMENT_MARK_RECEIVED`): the
  payment's provider refunds (manual: the member records the money returned) and a refund entry (`entry_kind`
  refund, `refund_of`) joins `crm.payment`; a fully refunded payment's commitment reads `refunded` (still
  `has_money`). Every money step of an invoice runs under its row lock.
- **Payment methods** (`/settings/payment-methods`, owner `PAYMENT_SETTINGS`): the manual method's name and pay-by
  instructions (printed on the agency's unpaid invoices), disconnect. `app/payments/providers.py` is the BYO
  abstraction (`PaymentProvider`: `refund`, `takes_cards`, `needs_external_account`, `currency`); `manual` and (AA-1072)
  `vn_gateway` have code, `stripe` (Connect Standard on the agency's own account, paused: root ADR 0019 b14) answers
  409 `provider_not_available`.
- **Web**: `components/invoices/InvoiceMoney.tsx` (record payment, payments, request refund) and `RefundActions.tsx`
  (approve / reject / mark refunded) on the trip's Invoices card; `RefundsPanel.tsx` on A-10 `/payments`;
  `PaymentMethodsForm.tsx` on Settings > Payments; BFF lines card AA-952 in `lib/bff.ts`.

## CRM-23 claim, hold extension, re-hold and reminders (AA-952 slice 4)

Design v3 §3.5 "Deposit link (C31, Fix A3, Fix A8)", F6 step 5, WF-9, U8; card CRM-23 R / E; migration crm-028; code in
`app/payments/claims.py` (pay window, claim, extension), `app/booking/rehold.py` (the re-hold, run by the Booking sync),
`app/payments/reminders.py` (run by the hourly date rule); decisions in `docs/implementation-notes/AA-952.md`.

- **The pay link is the client's proposal link.** An accepted page (`GET /crm/public/v1/proposal`) lists the issued
  deposit / balance invoices of that version under `payments`: number, total, what is left, due date, the **pay window**
  and, only while it is open, the agency's own manual pay-by details. The window is derived, never stored: a deposit
  invoice of an accepted builder version with an AA hold is payable until `crm.proposal.hold_expires_at`; past it the
  state is `expired` (no details, no claim, no card session). While the client still has newer booking terms to
  re-accept, a deposit's open window reads `terms` instead (same: no details, no claim, no card session; R34). Other
  invoices have no window. The agency's trip invoice list and receivables carry the agency `timezone`: the claim
  note's deadline is printed in it with a zone label. The invoice status
  `expired` stays unused (crm-024 has no expired -> paid transition for the money that still arrives).
- **"I have paid"** (`POST /crm/public/v1/proposal/invoices/{id}/claim`, the link cookie): open -> `payment_claimed`
  (`claimed_at`), the trip owner's urgent task `verify_transfer` (due at the hold's expiry, else in 2 days), bell
  `invoice.payment_claimed`; then, outside any transaction, **one** `POST /holds/{id}/extend` (reason payment_claimed,
  Idempotency-Key `crm-invoice:<id>:extend`; Booking adds its length, 72 h, once per hold) and, in a second
  transaction, `hold_extended_at` + the proposal's hold expiry + the task's due time (or Booking's code in
  `hold_extend_code`; `booking_unavailable` is retried by the Booking sync). 410 `pay_link_expired` past the window; 409
  `terms_reaccept_required` before any write while newer terms wait (no task, no bell, no extension used). A
  second press answers the same. The agent: record the payment, or "Not received" (`POST /invoices/{id}/reject-claim`:
  back to open, task skipped; a claimed invoice is not voided: 409 `invoice_claimed`).
- **Card button** (`POST /crm/public/v1/proposal/invoices/{id}/card`): the window is checked before any provider: 410
  `pay_link_expired` past it (never a payment session), else 409 `card_not_offered` without an online account of the
  agency's own / `provider_not_available` (Stripe, paused); AA-1072: with the agency's VNPay account, a hosted payment
  (section "Book now and online payment" below).
- **Money after the hold ran out** (also after the extension): `record_payment` that completes such a deposit sets
  `rehold_requested_at` and settles nothing (`rehold_pending: true`). The Booking sync (`invoice_holds`, after the
  accepted sets) reads Booking's hold (still active: `hold_live`), else quotes the same AA lines and holds them again
  (`crm-invoice:<id>:rehold`, the quote written down first so a lost answer replays) only when none is sold out and the
  new net total is not above the expired hold's. Held: the proposal's hold columns and the AA lines move to the new
  hold (cost from its prices), the invoice is paid, its row settled (Booked, portal, confirm; the confirm query takes
  the proposal's current hold too), bell `invoice.reheld`. Not held (`sold_out`, `price_higher`, `price_changed`,
  `not_found`, ...): `paid_unfulfillable`, one refund request per payment for what is left to ask back (proposed by
  `job:crm_booking_sync`, decided under the §4.6 matrix), urgent task `payment_unfulfillable`, bell
  `invoice.unfulfillable`; the row stays due, the trip does not move. Booking's `rehold_limit` waits for the next UTC
  day (26 h at most). A late `hold.expired` of a hold the lines left no longer marks them expired.
- **Overdue reminders** (agent first, U8): the hourly `crm_stage_date_rule` gives the trip owner a bell
  `invoice.overdue` and one open task `invoice_overdue` for an issued, open invoice with money due past its due date:
  the day after, then every 3 days, at most 5 (`last_reminded_at`, `reminder_count`). Claimed and re-holding invoices
  are not reminded; the CRM never writes to the client by itself. The task is closed (`done`) once the trip has no
  invoice left to chase: at once when an invoice is paid in full (also sent to the re-hold, and the re-hold's
  answer) or voided (`reminders.settled`), and by the date rule's sweep for any other way (`reminders.close_settled`).
  A claimed invoice keeps it open ("Not received" resumes the chase).
- **Web**: `components/proposals/ClientPayBox.tsx` (accepted text and day-by-day pages), the claim note and "Not
  received" on the trip's Invoices card, the note on A-10 `/payments`; BFF line `invoices/{id}/reject-claim`.

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
  member over a sliding hour; the job sampler logs the peak (`reveal_peak`), and 60 in an hour logs `crm_bulk_reveal`
  and raises the ops check `bulk_reveal` (one AA ops e-mail per member and hour; S263, was an Infra alarm). A traveller sees their own record only through
  `app/travellers/self_reveal.py` `traveller_self_reveal(session)` (masked passport, own trip only).
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
  integration tests. `crm_support_reader` (AA-971, crm-033) reads named columns of four non-F1 tables only (`tenant`,
  `tenant_settings`, `trip` without its title, `trip_stage_history`) and writes nothing. `v_insight_fact` shows one
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
  DEFAULT partition: an insert with no partition fails, which fails the request; ops check `audit_partitions`
  when next month has no partition). A partition is dropped only when it ends at or before the start of the UTC month twelve months ago **and**
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
- **Checks** (the job sampler, `app/observability/ops_alerts.py`; CloudWatch metrics until S263): `audit_export_lag`
  (export 180 minutes or more behind), `audit_partitions` (next month has no partition) and `audit_verify_lag`
  (largest exported-minus-verified gap over agencies at 50 hours: one agency failing verification stops drops for all).

## F4 roles and team (AA-939, CRM-11)

- **Permission matrix** (`app/permissions.py`, design v3 §4.6): every agency action x owner / agent / assistant with
  rules ALLOW, DENY, OWN, ASSIGNED, THRESHOLD (refund threshold from `tenant_settings`); one table, pinned cell by cell
  in `tests/unit/test_permissions.py`. Doors: `tenant_tx(action=)` (role level) and `permissions.require()` (object
  facts). Rows added after the design: `aa_confirm` (AA-947) and `aa_tariff_view` (AA-1030, the AA prices page and
  CSV: owner, agent, like `view_margin`; owner only = one row change, never wider than `view_margin`, a unit test)
  and `discount_approve` (AA-1066, owner only: a proposal discount above the agency limit or a sale below cost).
  `/me` carries `permissions` (today `aa_tariff_view`) so the web hides what the API would refuse. Membership writes are also refused by the database for anyone but an owner (crm-008).
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
  claimed `(iss, sub)`). An account that was never signed in to (`FORCE_CHANGE_PASSWORD`) gets a new temporary password
  (`AdminCreateUser` RESEND, which Cognito also mails) when a **new** invitation to its address is claimed (S263); an
  account with its own password is never reset. A claimed invitation claimed again answers `already_claimed` and
  changes nothing (`invitation_claim` denied, reason `already_claimed`): a second tab or a double submit never rotates
  the password the first claim shows. A person who never saw it gets a new invitation (the owner revokes and re-invites;
  AA withdraws a first-owner invitation in the L1 console). The API's partner-pool calls (`app/members/directory.py`)
  are AdminCreateUser, ListUsers, AdminUserGlobalSignOut and (S263, root ADR 0006b amendment A1) AdminDeleteUser
  (IAM allows AdminDisableUser too; unused).
- **Partner-account clean-up** (S263, AA-939 follow-up; `app/members/accounts.py`, job `crm_partner_accounts`): an
  invitation that ends unused (withdrawn, revoked, expired, or its synthetic test agency deleted) left the claim's
  partner-pool account behind in `FORCE_CHANGE_PASSWORD`. The clean-up deletes such an account (`AdminDeleteUser`)
  only when a CRM invitation claim bound it, no CRM member row (`active` / `invited`) in any agency, no live or
  accepted CRM invitation and no live Content studio invitation or membership names it, all its invitations ended more
  than 7 days ago (at once for a trashed agency's), and Cognito, read again right before the delete, still says
  `FORCE_CHANGE_PASSWORD` and no change since those invitations ended. A `CONFIRMED` account is never deleted
  (the directory refuses it). Reads: every agency of the audit registry through `partner_account_tx()` (trashed ones
  too, `tenant_isolation` per agency) and `crm.studio_invitation` through `studio.account_links()` (crm-039 lets the
  actor `job:crm_partner_accounts` read it; the read refuses to run before crm-039). Each delete: a
  `partner_account_delete` event (invitation ids, reason `never_signed_in`) in every agency whose invitation it was.
  Runs daily and once per test agency delete (queued in its transaction); `PARTNER_ACCOUNT_CLEANUP_DRY_RUN` (default
  on; Dev off) and at most 50 deletes per run. By hand in the container: `python -m app.members.accounts [--apply]
  [--limit N] [--tenant <uuid>]` (dry run unless `--apply`).
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
  `/join`, never in a URL; both answer cookies name their invitation, `GET /join` drops another invitation's, an
  `already_claimed` answer goes into its own notice cookie so it never overwrites a password, and "Open invitation"
  submits once). Runbooks (lost second factor, rollout order): `docs/implementation-notes/AA-939.md`; the S263 join fix:
  `docs/implementation-notes/AA-939-join-fix.md`.
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
  `ticket:<REF>`): create agency (`app.agencies.provision_agency` create-only, no member; 201, an existing id 409 `tenant_exists` with nothing written; an id whose agency is in the trash, e.g. a deleted test agency, 409 `tenant_deleted`: the id is never reused, create a new CIS tenant (AA-1031, crm.ops.v1 1.9)), lock / unlock
  (active <-> locked; a no-op writes nothing), first-owner invite (409 `owner_exists` / `owner_invite_pending` /
  `has_travellers` (any traveller row incl. trashed, or any stored inquiry) / `member_limit_reached`; the shared
  `team.create_invitation`; the address never stored in clear, logged or returned), allowed features (crm.ops.v1
  1.11, AA-1028: written only while the agency is `managed_by` ops, never projected from CIS; once the CIS account feed
  has projected it, 409 `entitlements_managed_by_cis`, audited as denied; GET `/entitlements` says `managed_by` cis /
  ops), insights opt-out (`tenant_entitlement.insights_opt_out`,
  crm-011), AA data requests (`agency_export` / `agency_deletion` -> a `crm.erasure_request` the owner must confirm in
  Settings > Data and privacy; no job until then; crm-011 trigger CRM18), job retry (failed `crm_intake_process` /
  `crm_dispatch` / `crm_offboard` of this agency only, 404 `job_not_found` otherwise; locked agency 409), owner
  recovery open (ticket required) / complete, member deactivation. crm.ops.v1 1.8 (S263): **withdraw the first-owner
  invitation** (`POST /tenants/{id}/owner-invite/withdraw`: AA's live owner invitations, pending or claimed and not
  accepted, become `revoked`; 404 `owner_invite_not_found`; active or locked agency) and **delete a test agency**
  (`POST /tenants/{id}/delete` with the agency name typed exactly: synthetic only, 403 `not_synthetic` on record
  otherwise; never the pilot agency, 403 `pilot_agency` on record (AA-969 d16, whatever its synthetic flag); 409 `has_members` (active or invited) / `has_travellers` / `legal_hold` / `tenant_status` (offboarding);
  revokes live invitations, crypto-shreds the agency keys (`KeyProvider.destroy`) and trashes the `crm.tenant` row, so
  no context opens it and the list leaves it out; `agency_delete` event; and queues the `crm_partner_accounts` run for
  its never-used partner-pool accounts). A real agency is deleted only by its owner's confirmed offboarding (AA-942).
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
  Every hold guard trigger reads the holds of the row's own agency through `crm.hold_row_of(OLD.tenant_id, ...)` /
  `hold_trip_of` / `hold_traveler_of` (crm-058, AA-1085), never through RLS alone: a foreign-key cascade runs as the
  owner without row security, so an RLS-only guard would see every agency's holds there. The guards therefore also
  check cascades (a partner delete onto a held trip's commission, a party row onto a held trip's flight / room /
  visa). Lint: `api/tests/unit/test_aa1085_hold_guard_tenant.py`.
  A hold placed by `platform:*` is released only by `platform:*`; a hold is never deleted nor trashed (its trash
  columns stay NULL), it only moves to released.
- **Erasure** (`POST /travelers/{id}/erasure`, owner): dry run by default; a real run bypasses the trash and leaves an
  `erasure_request` receipt (backup_purge_by = +30 days). AA-967 (`app/portability/subject.py`): the dry run also
  answers what is kept (crm-025 signed evidence, `*_kept`), the exceptions (`signed_agreements_claim_period`,
  `accounting_records`) and the exports it would withdraw. A logged data request (`data_request_id`) is locked and
  checked first: open, kind `erasure`, about this client or none yet (409 `data_request_closed` /
  `data_request_not_erasure` / `data_request_other_client`). A real run withdraws the agency's and the client's
  exports, erases, **verifies** (the same erase as a dry run must answer `subject_not_found`, else 503
  `verify_failed` and nothing is kept), writes the receipt (format 2: counts, kept, exceptions, exports withdrawn,
  backup_purge_by, copies outside AA, verify) with `verified_at`, closes the logged request, and only then deletes the
  bucket objects (the export ZIPs, the client's documents): a refusal on the way deletes no file (`pending=` on
  `exports.withdraw` / `erase.erase`, then `erase.delete_objects`).
- **Offboarding** (`/lifecycle/offboarding`, owner; `app/lifecycle/offboarding.py`): request (dry-run counts, shown as
  the deletion report) -> export job (JSON lines format 2: every table of the catalog as the portability ZIP has them,
  `app/portability/tables.py`, personal fields decrypted, indexes / F2 pointer / secrets left out, sealed with the
  agency `export` DEK, documents bucket `exports/tenants/<tenant>/<request>.jsonl.enc`, 7-day bucket rule) -> download
  (audited) -> typed confirmation (the agency id) -> **cool-off** (AA-967: `OFFBOARDING_COOL_OFF_HOURS`, default 72;
  `erase_not_before` + `confirmed_by`, the erase job queued for that time; the agency works as usual and the owner may
  cancel; 0 = the AA-942 immediate start) -> agency `offboarding` -> erase job: the portability exports withdrawn,
  `erase_subject('tenant')`, a canary sealed under the vault DEK, `KeyProvider.destroy` (crypto-shred;
  other tasks' DEK caches expire within `DEK_CACHE_TTL_SECONDS`, stated on the receipt), the canary must no longer open
  (else 503 `verify_failed`: rolled back, the request `failed`), the receipt's `verify` block + `verified_at`, agency
  `offboarded`, and last the bucket objects (documents, offboarding export, portability exports; a bucket error there
  is retried by the worker, not a refusal). The audit and its export registry / chain stay (AA-940 offboarding path). An erase
  the database refuses (a hold placed after the confirmation, CRM16) deleted nothing: the request is `failed`, the
  agency is `active` again and the owner cancels the request. An erase job that meets DRY RUN (switched on after the
  confirmation) is handled the same way (`failed` / `deletion_dry_run`).
- **DRY RUN**: `LIFECYCLE_DRY_RUN` (default true) makes the daily run count only and every owner erasure / offboarding
  erase answer 409 `deletion_dry_run` with the counts.
- **Portability exports** (AA-967, `/crm/v1/data/exports`, `app/portability/`, web Settings > Data and privacy "Export
  your data"): owner only (matrix `agency_export` / `client_export` / `data_settings`, plus the service's owner check
  with a committed `denied` event). Request (whole agency or one client) -> `crm_data_export` -> `ready` with the ZIP's
  SHA-256 shown on the page -> `POST .../link` (a random token, only its SHA-256 stored, 5 minutes, single use) ->
  `GET .../download?token=` (both digests checked, `application/zip`, `X-Content-SHA256`; audited `export_download`;
  a used / late / wrong token 403 `link_invalid`) -> expired after 7 days (410 `export_expired`). AA staff never
  download: the route class AGENCY answers every AA token 403 and the L1 console only raises requests (AA-941). A client
  export takes at least what the traveller erase reaches (`tables.CLIENT_WHERE` = its WHERE clauses; a unit test
  compares them with the latest `erase_subject`): the rows that name the client (`traveler_id`), the F2 profile rows
  (by token), both relation ends, guardian signatures, the proposal acceptance (those columns only), call slots of
  their submissions, dispatch attempts, their conversations' messages, plus the kept signed evidence and their trips.
  Documents up to 25 MiB per export (the ZIP is built in memory in the 512 MB API task; the rest is listed
  `skipped_size_cap`).
- **Data-request log** (AA-967, `/crm/v1/data/requests`, `app/portability/requests.py`): the agency logs what its
  clients ask (kind, channel, client, received, due = +30 days by default, up to +92), answers with a client export or
  erasure from the same row, extends, closes with an outcome. Codes and dates only; kept like the receipts.
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

## CRM-33a AI gateway, Settings > AI, drafts (AA-964)

- **Gateway v2** (`app/llm/gateway.py`, root ADR 0008a §5-6, root ADR 0009 §8): the order of Slice 1 / F3 with a
  per-agency cap in front (`RATE_LIMIT_AI_PER_MINUTE`, default 6, `app/ratelimit.py`: over it `rate_limited`, logged,
  nothing scrubbed or paid). Each row follows the ledger v2 contract (crm-032: `app = 'crm'`, attempt, provider request
  id, connection, wire id, family, cache tokens, pricing mode, `prompt_hash` of the scrubbed prompt, `prompt_version`,
  `job_id`). Every Bedrock Guardrails `ApplyGuardrail` attempt of the scrub is its own row (stage `crm_scrub`, role
  `guard`, `units` text units at USD 0.10 per 1000, `pricing_mode` `code_table`, or `estimate` when the attempt failed
  or timed out), written before the call's row, so the budget counts it. Budget alerts (R5): the first time the month
  reaches 75 / 90 / 100 % the gateway stores the level (`tenant_settings.ai_alert_month` / `ai_alert_pct`), writes one
  `ai_budget_alert` audit event (reason `pct_75` ...) and the result / settings carry it. Guardrail down (acc2): fail
  closed, "reply by hand", log `crm_scrub_unavailable`; no second guardrail elsewhere.
- **Factory** (`build_gateway`, `app.state.llm_gateway`): `CRM_LLM_PROVIDER=stub` (default; deterministic, no network,
  cost 0) or `bedrock` (`app/llm/providers/bedrock.py` `BedrockConverseProvider`: Converse with our fixed system block
  and the scrubbed prompt only, no tools, one attempt per route entry, an injectable client, the CRM-only invoker role
  `CRM_LLM_ROLE_ARN` when set; the service refuses to start without `CRM_LLM_MODEL_ID` and both per-million-token
  prices). Since S263 the provider plays the stage's route (see "Stage profiles" below). The F3 scrubber whenever
  `SCRUB_GUARDRAIL_*` is set or the provider is real (without a guardrail it refuses every scrub); the stub scrubber
  only with the stub provider and no guardrail.
- **Bypass guard** (AA-642 L6, `api/tests/unit/test_gateway_bypass.py`): any Bedrock method (`converse*`,
  `invoke_model*`, `apply_guardrail`, `create_model_invocation_job`, called or passed uncalled), vendor SDK call or
  import (`openai`, `anthropic`) or vendor endpoint string (`api.anthropic.com`, `api.openai.com`, `bedrock-runtime`)
  in `api/app` / `api/scripts` outside `app/llm/providers/bedrock.py` and `app/privacy/guardrails.py` fails CI; so
  does a vendor AI SDK import or endpoint in `web/` or `portal/`.
- **Settings > AI** (`app/llm/agency_ai.py`, `app/routers/ai.py`, web `/settings/ai`): `GET /crm/v1/settings/ai`
  (every role, action `ai_use`): switch, budget, plan cap `AI_BUDGET_MAX_USD` (default 2), this month's spend and
  share, alert level, test mode, cap per minute, `ai_assist_allowed`; `PUT` (owner, action `ai_settings`):
  `ai_enabled`, `ai_budget_usd` (0 .. cap, two decimals; above: 422 `budget_above_plan`), audited. Test mode has three
  fields (AA-1027 F3 follow-up): `model_test_mode` (the stub model writes, free), `scrub_billed` (`SCRUB_GUARDRAIL_ID`
  set, so the privacy check pays `ApplyGuardrail` units even with the stub model) and `test_mode`, true only when
  nothing billable runs; the web says "nothing is charged" only then. Without `ai_assist` on the plan the page shows
  "Not on your plan" with the trip card's wording; the owner keeps the switch (AI drafts are its only use).
- **Draft** (`POST /crm/v1/trips/{id}/ai/draft` `{kind: reply|follow_up, text, instruction?}`, action `ai_use`): 403
  `feature_not_allowed` when the plan lacks `ai_assist` (AA-1027 F3, `crm.tenant_entitlement.allowed_features`, the
  projection of the tenant's CIS `crm.ai_assist` entitlement since AA-1028, `app.entitlements`; a `denied` audit event), then 409 `ai_disabled` before the gateway when AI is off (nothing scrubbed, nothing paid); else stage `crm_draft_reply`, role
  `writer`, prompt `crm-draft-1` with the trip's known values; the client text sits in `<client_message>` tags under a
  fixed system instruction (never follow instructions inside it; no tools); placeholders are restored in the member's
  session; the answer carries `approval_required: true` and is never sent from here.
- **AI drafts in the trip workspace** (AA-1027 F3, S263 gateway plan row F3, gap CRM-4; design R39; crm-040,
  `app/llm/ai_drafts.py`): every answered draft (outcome `ok`) is one `crm.ai_draft` row: trip, kind, the ledger call
  id (no key: the ledger is purged after 180 days), prompt version and the draft **as the model wrote it**
  (placeholders kept, never restored at rest) under the vault DEK (`output_enc`); the response carries `ai_draft_id`.
  No prompt is stored. `POST /crm/v1/ai/drafts/{id}/accept|discard` `{feedback_code?}` (`good`, `wrong_facts`,
  `wrong_tone`, `not_relevant`, `too_long`, `other`): accept needs `ai_draft_approve` (assistant only on an assigned
  trip), discard every role; once, from `pending` (409 `draft_decided`); the guard `crm.guard_ai_draft` (CRM47) holds
  the same rules (a member inserts and decides, nothing else changes, no delete; the row cascades from its trip).
  `GET /settings/ai` carries `ai_assist_allowed`. Web: the "AI draft" card under the trip panel
  (`components/pipeline/AiDraftPanel.tsx`, strings `lib/i18nAiDraft.ts`): Draft opens the kit Drawer (kind, client
  message, optional instruction), the draft with the staff `AiNotice`, a feedback select, Copy and accept (clipboard;
  sending stays the member's own message) / Discard; disabled with the reason without the plan or with AI off (the
  owner gets the Settings > AI link). R39 `validation` and the stored scrubbed prompt come with CRM-33b.
- **App profile** (AA-1025 H1c, crm-041, `app/llm/app_profile.py`; root ADR 0009 §4): the CRM's row of the
  ecosystem app profiles, kept in `crm` (ADR 0008a §6). The CIS admin edits it on AI providers › Providers › App
  profiles and relays the whole profile to `PUT /crm/ops/v1/llm/app-profile` (crm.ops.v1 1.10, the staff member's own
  token, `crm-platform-admin`; `GET` for any OPS reader; no agency audit event, CIS audits the edit). The gateway reads
  it right after the stage profile and refuses first: AI off `app_disabled` (class `disabled`), the daily budget of
  every agency together (`crm.llm_usage_daily`, guardrail rows included) reached `app_budget_exhausted` (class
  `budget`), that spend unreadable `spend_unreadable`; one refused row at 0 USD, the draft says "reply by hand"
  (`aiDraft.fallback.paused` / `.budget`). The read fails open (no table, no row or a read error = AI on, no daily
  budget); each agency's own monthly budget still applies after it. The CRM's own signal of that budget is the job
  sampler's ops checks `llm_app_spend_near` / `llm_app_spend_cap` (one AA ops e-mail per check and UTC day on the
  platform alerts topic, the recipients of the CIS ops e-mail): CIS cannot read the CRM ledger (ADR 0008a), so there
  is no CIS bell for it.
- **Stage profiles and the shared core** (S263 gateway plan F1 + F2, crm-037, `app/llm/profiles.py`): each CRM stage
  has a profile in `crm` (`crm.llm_stage_profile`: role, kill switch `is_active`, `max_attempts` <= 6,
  `route_deadline_s`, degrade mode `manual_reply_needed`) and a route (`crm.llm_stage_route`: Bedrock model ids in
  order, family = maker, catalog prices per million tokens). The gateway reads it first in the call's transaction: no
  profile `stage_unrouted`, unreadable `profile_unavailable`, another role `profile_mismatch`, switched off
  `stage_disabled`: one refused row at 0 USD, nothing scrubbed or sent. Then the route runs entry by entry, one row per
  attempt (`attempt_no`), until one answers; no new attempt after the deadline (`deadline`) or once a billed failure
  used the budget up (`budget`). The vendored core decides: `errors.classify` + `ledger.failed_outcome` (refused
  `access_denied` / `quota_exhausted` / `throttled`, else `error` / `other`), `truncated` for a reply cut at its token
  limit (never `ok`; the draft says "reply by hand"), `billed_unusable` for a billed answer without text,
  `budget.decide` (an unreadable month refuses `spend_unreadable`), `pricing.quote` for Bedrock (`override` = the
  `CRM_LLM_USD_PER_MTOK_*` prices of `CRM_LLM_MODEL_ID`, else `catalog` = the route row, else `code_table`, else the
  high `estimate`, never 0). The stub plays each entry (its model and family, cost 0); `[stub:truncate]` /
  `[stub:fail-first]` in a prompt make it cut its reply / throttle the first entry ($0 Dev checks). Prices are not
  read from the CIS contract views (ADR 0008a §1: no grant outside `crm`): the route rows carry the catalog price.
- **CIS sees the rows** (`crm.ops.v1` 1.6; 1.7 adds the statuses `billed_unusable` / `truncated`): `GET /crm/ops/v1/llm/calls?from&to&tenant_id&stage&status&limit&cursor`
  (any OPS reader; at most 31 days, 500 rows; keyset cursor `<at>~<call id>`): newest first across every agency, ids,
  codes and numbers only (no receipt per-kind counts, no member, no text); one `ops_read` event (reason `llm_calls`) in
  each agency whose rows were returned.
- **Shared core, code only** (AA-642 S8, root ADR 0009 decision 13): `api/app/aa_llm/` is a byte copy of AA-CIS-App
  `shared/aa_llm/` (error classes, quote, ledger v2 row shape, budget decision, ports), pinned by `api/aa_llm.lock.json`
  and synced by CIS `.github/scripts/llm-core-sync.sh` / the Llm Core Sync bot (PR `bot/llm-core-sync`). Never edit
  it here; `tests/unit/test_aa_llm_copy.py` checks it offline against its `MANIFEST.json` and the lock. The CRM keeps
  its own control plane and ledger (`crm.tenant_settings`, `crm.llm_call`, ADR 0008a): no module here may use the
  core's `insert_sql()` (it targets `shared.llm_call_log`) or the CIS contract views (guarded by that test). Coverage
  omits the copy (tested in CIS).

## AI boundary for high-risk advice (AA-820)

The CRM sells non-AA ancillaries (flights, insurance, visas, transfers, fees; root ADR 0019 b6), so it is where an AI
feature could drift into advice the Anthropic Usage Policy (effective 12/11/2026) calls high-risk. The rule:

- **No CRM AI feature advises or decides about a specific client on insurance (choice, cover, underwriting or
  claims), visas and immigration, financing, instalments or credit, refunds or claims, unless a qualified agency member
  reviews each recommendation, with authority to change it, before it reaches the client, and the client is clearly
  told that AI was used.** The disclosure need not name the provider or the model.
- General travel information that does not apply a rule to one client's situation ("a visa on arrival exists for many
  nationalities") is not advice (the policy's exclusion for general information).
- Today this holds by design: the only AI feature is the reply / follow-up draft, which a member copies and sends by
  hand (`approval_required: true`, CRM-33a above), under the staff `AiNotice`. A new AI feature that touches these
  topics (an insurance or visa suggester, an auto-reply, a traveller-facing chat) must add the review step and the
  disclosure before it ships, and its design cites this section.
- The agency-facing wording is in the terms of use (`web/lib/i18nTerms.ts`, keys `terms.ai.highRisk` and
  `terms.ai.prohibited`, page `/legal/terms#ai`), which summarise the AI Schedule v1.0 (root
  `docs/legal/tenant-ai-schedule.md`, approved by the tech lead under ADR 0019 b20; counsel reviews it before the first
  paying agency). The Content studio terms §3 (`portal/app/terms/page.tsx`) carry the same Schedule; both take its
  version and the provider policy links from an `aiTerms.ts` copy (`portal/app/_legal/`, `web/lib/`), kept equal by
  `api/tests/unit/test_portal_guards.py`. AA-Booking `CONTEXT.md` holds the same rule for Booking.

## AA support access L2 + break-glass L3 (AA-971, CRM-13b)

Design v3 §4.8 (L2, L3), §2.3 "Support banner", S-04, WF-12; root ADR 0006b decision 6; crm-033.

- **Owner grant (L2)** (`app/support/grants.py`, `/crm/v1/support-access`): every role reads the state (the banner);
  the owner allows AA support for 1 to 7 days (default 1), read only, "show identity" off by default, and ends it any
  time ("End now": the grant is revoked and the live support session with it; the next staff read is 403). One live
  grant per agency; a grant's window and scope never change (end it, grant again). Web: Settings > Data and privacy >
  AA support access (`components/settings/SupportAccessView.tsx`) and the shell banner
  (`components/support/SupportBanner.tsx`: amber for support, red for emergency, not dismissible, [End now] for the
  owner on support only).
- **Staff side** (`app/support/sessions.py`, `app/routers/staff.py`): `crm-support` lists the agencies that allow
  support now and starts a session with a ticket (`SUP-1042` pattern; one staff member at a time per agency; the
  session ends with the grant or after 8 hours); `crm-ops` opens **break-glass (L3)** without a grant: a reason code
  (`security_incident`, `data_corruption`, `legal_request`, `owner_unreachable`, `service_outage`), a ticket, 15 to 60
  minutes, an active or locked agency, identity **never** shown (crm-033 CHECK: the in-app path has no MFA step-up and
  skips the owner's consent, so F1 names wait for the AWS-level role below). After the commit: the structlog warning
  `crm_breakglass_opened` (ids and codes) with the ops check `breakglass_opened` (one AA ops e-mail per session, S263;
  `breakglass_owner_not_mailed` when no owner could be mailed) and the AA
  system mail `breakglass_notice` to every active owner (`owner_notified_at` once sent). The owner cannot end an
  emergency session; it closes by itself.
- **Reads** (`app/support/reads.py`, `support_tx()`): `GET /crm/staff/v1/agencies/{id}/overview | trips | trips/{trip}`;
  ids, codes, dates, the agency name, trips without their title; the party's display names only when a support
  session shows identity (one `reveal` event naming the travellers). One `support_read` event per call (a list names the ids
  shown). There is no export, send, role, integration or settings route on the staff class (unit test), and the reader
  role has no write privilege (crm-033 guard, integration test).
- **Audit**: actor kinds `support` / `breakglass` (the owner's access log filters "AA support" / "AA emergency
  access"); actions `support_grant`, `support_revoke`, `support_session_start`, `support_session_end`,
  `support_read`, `breakglass_open`, `breakglass_close`, `breakglass_owner_notice`, plus `reveal` and `decrypt`
  denials as usual.
- **Staff UI (AA-1019)**: `/staff/support` (agencies that allow support + start with a ticket; break-glass picker by
  name from `GET /crm/staff/v1/breakglass/agencies`, reason, ticket, minutes, a confirmation) and
  `/staff/support/<agency>[/trips/<trip>]` (amber / red banner with End session, overview, trips, the caller's
  sessions), section "AA staff console".
- **Not built here** (follow-ups): the AWS-level break-glass of design §4.8 L3 (IAM role `aa-crm-<env>-breakglass` with MFA and a
  `tenant_id` session tag; the KMS statement `CrmBreakGlassOneAgency` already waits for it in AA-CIS-Infra
  `crm_data.tf`; CloudTrail Decrypt copied into the agency audit).

## AA staff console (`web/app/staff`, AA-1019)

S-02 and S-04 for AA staff, on the agency host under `/staff`, signed in with the WORKFORCE `crm-staff` client (root
ADR 0006b decision 6). Decisions: `docs/implementation-notes/AA-1019.md`.

- **Own session** (`web/lib/staffSession.ts`): cookies `__Secure-aa_crm_staff_s` (Path `/staff`), `_r` and `_tx` (Path
  `/staff/auth`), sealed with `STAFF_SESSION_SECRET` (never the agency key, which the Content studio also holds) and the
  seal purposes `staff-*`; the access token must belong to the crm-staff client; the token's staff groups are kept for
  the pages. The agency session is never read on `/staff`, the staff session never on agency pages, `/bff` or `/auth`.
  Routes `/staff/auth/{sign-in,callback,refresh,sign-out}` (sign-out POST, same-origin only, ends the WORKFORCE
  managed-login session and leaves the agency session alone); `/staff/signed-out` (`?reason=failed|unavailable|no_group`).
  A WORKFORCE user without `crm-ops` / `crm-support` is signed out at the callback.
- **Gate** (`web/proxy.ts`): `/staff*` is decided before the agency rule and only by the staff session; no valid staff
  token -> `/staff/auth/refresh?next=`; the client host never serves `/staff`. The CSP `form-action` adds the WORKFORCE
  login domain on staff pages.
- **Staff BFF** (`web/lib/staffBff.ts`, `web/app/staff/bff/[...path]`): listed (method, path) pairs onto
  `/crm/staff/v1` only, each with its groups (403 `missing_group` before any upstream call), query parameters allow-listed
  per path, 8 KiB bodies, cross-site writes refused first. Every call, GET included, must be the console's own fetch:
  header `x-aa-staff-console: 1` (`web/lib/staffClient.ts`) and a `Sec-Fetch-Site` of `same-origin` when sent, else 403
  before any cookie is read — some staff GETs act (a release view is counted and audited, the manifest export is
  audited, S-04 reads are access-log lines) and the Lax staff cookie rides on navigations and same-site subresources.
  Server components call the same list (`web/lib/staffApi.ts`); the pages that count or audit on render (one release,
  one agency, one trip) show an "Open" step instead of reading when reached from another origin
  (`components/staff/OpenStep.tsx`, `Sec-Fetch-Site` cross-site / same-site).
- **Configuration** (task environment, AA-CIS-Infra `crm_web.tf`): `STAFF_COGNITO_DOMAIN`, `STAFF_COGNITO_CLIENT_ID`, and
  from the web secret `STAFF_COGNITO_CLIENT_SECRET` (`staff_cognito_client_secret`), `STAFF_SESSION_SECRET`
  (`staff_session_secret`). Any missing = the console says it is not set up; `/healthz` reports `staff_auth_configured`
  and the web post-deploy verify requires it.
- **Pages** (kit only: `app/_kit/local/StaffShell.tsx` over `PageFrame`, DataTable, Field controls, ConfirmModal; EN with
  VI in `web/lib/i18nStaff.ts`): `/staff/ops`, `/staff/ops/<agency>/<trip>`, `/staff/support`,
  `/staff/support/<agency>`, `/staff/support/<agency>/trips/<trip>` (sections above).
- **Dev E2E staff user**: AA-CIS-Infra `tools/identity/crm_dev_identity.py e2e-staff` (root ADR 0006a A10c), synthetic
  agencies only and never the pilot agency (route classes table). AA-1086 G9: on Dev every agency is synthetic, the
  pilot included, so `app.tenancy.E2E_SYNTHETIC_SQL` and the studio relay's `SYNTHETIC_AGENCY_SQL` both read
  `is_synthetic IS TRUE AND is_pilot IS NOT TRUE` (the marker CIS sets and `crm_entitlement_pull` projects, crm-044).

## Dev seeder (`api/app/seed/`, AA-935)

`python -m app.seed` inside the API container (ECS Exec on `aa-crm-dev-api`; it has the `crm_app` DSN): DRY RUN by
default (reads state, writes nothing), `--apply` creates or converges the synthetic agencies of
`app/seed/catalogue.py` (fixed UUIDv5 ids, one with a Vietnamese name, default locales vi-VN / en-AU / en-US; AA-1086
G1: plus the pilot rehearsal agency `pilot-rehearsal-01`, owner + agent, whose id is the one AA-CIS-App's
`e2e_fixtures rehearsal-tenant` provisions the CIS tenant under, root `docs/ops/runbooks/pilot-rehearsal.md`) and the
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
`KMS_SEAL_KEY_ID` (AA-955: the e-sign seal key `alias/crm-<env>/seal`; without it the HMAC dev stub signs on dev / test /
local and signing answers 503 `esign_not_configured` on stg / prod),
`DEK_CACHE_TTL_SECONDS`, and the public intake (AA-936) `INTAKE_PAGE_ORIGINS` (the client host origin; empty on Dev =
every submission 503 `intake_not_configured`), `RATE_LIMIT_INTAKE_PER_MINUTE` (default 5 per address; also every anonymous client-page write),
`RATE_LIMIT_PORTAL_WRITE_PER_MINUTE` (default 30 per address: traveller portal writes with a session that proved valid;
not set on Dev, the code default applies), `RATE_LIMIT_BOOKING_PRICE_PER_MINUTE` (default 20 per address: "Book now"
price checks; not set on Dev, the code default applies),
`RATE_LIMIT_INTAKE_FORM_PER_HOUR` (default 200 per form; AA-1092: one bucket per form id, shared by the public form and
its sales partner links), and the documents bucket (AA-937) `DOCUMENTS_BUCKET`, the
audit export (AA-940) `AUDIT_BUCKET_COMPLIANCE` / `AUDIT_BUCKET_GOVERNANCE` (both or neither) and
`AUDIT_RETENTION_DRY_RUN` (default true), and (AA-939) `COGNITO_PARTNER_POOL_ID`, `PARTNER_WEB_ORIGIN`,
`SES_FROM_ADDRESS`, `SES_CONFIGURATION_SET`, `INVITATION_TTL_HOURS` (default 168), `RATE_LIMIT_CLAIM_PER_MINUTE`
(default 10), and the F3 guardrail (AA-938) `SCRUB_GUARDRAIL_ID`, `SCRUB_GUARDRAIL_VERSION`,
`SCRUB_GUARDRAIL_REGION`, `SCRUB_DETECTOR_TIMEOUT_MS`, and the data lifecycle (AA-942) `LIFECYCLE_DRY_RUN` (default
true) and (AA-967) `OFFBOARDING_COOL_OFF_HOURS` (default 72, 0..720; not set on Dev: the default applies), and the FX source (AA-944) `FX_PROVIDER` (`synthetic` by default on dev / test / local, `live` on stg / prod;
not set on Dev today), the CIS membership feed (AA-978) `CIS_API_URL` (the CIS API origin) and `M2M_CLIENT` (secret
`aa-crm/<env>/m2m-booking-client`, the JSON `{client_id, client_secret, token_url, scopes}`; either empty = the pull
logs `not_configured`; AA-947: the same client asks `cis.catalog/read` for the catalog cache and `booking.b2b/*` for
Booking), the Booking API (AA-947) `BOOKING_API_URL` (the Booking API origin; empty = AA requests stay "AA pending" and
the product look-up is 503 `booking_not_configured`), `BOOKING_ADMIN_URL` (AA-956: the Booking staff admin origin; S-02 links to `<this>/b2b-desk` to flag an incident; empty = no link), and mail (AA-945) `CRM_INGEST_BUCKET`, `CRM_INGEST_DOMAIN`, `MAIL_OAUTH_REDIRECT_BASE` and the
secret `MAIL_OAUTH` (`crm/<env>/mail-oauth`, mapped only once it has a value), and the portal (AA-946)
`PORTAL_OTP_DEV_ECHO` (default false; refused outside dev / test / local; on, the portal code answer carries the code
for the synthetic Dev agencies that have no mailbox), and the AI gateway (AA-964) `CRM_LLM_PROVIDER` (default `stub`),
`CRM_LLM_MODEL_ID`, `CRM_LLM_USD_PER_MTOK_IN` / `CRM_LLM_USD_PER_MTOK_OUT`, `CRM_LLM_REGION`, `CRM_LLM_ROLE_ARN`,
`CRM_LLM_MAX_TOKENS`, `RATE_LIMIT_AI_PER_MINUTE` (default 6) and `AI_BUDGET_MAX_USD` (default 2); on Dev, AA-CIS-Infra
`crm_llm.tf` (AA-1027 F4) sets the provider `bedrock`, the model Amazon Nova Lite on acc2 with its catalogue prices, the
region and the output cap, and grants the task role `bedrock:InvokeModel` on that inference profile only (no
`CRM_LLM_ROLE_ARN`: acc3 is not used while it refuses every model). Meanings and defaults:
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
  logs the ERROR line `crm_membership_feed_stuck` and raises the ops check `membership_feed_stuck` (one e-mail a UTC
  day); a refused event raises `membership_apply_refused` (one per event). Both were CloudWatch metric filters until
  S263. The L1 agency summary carries the same word and the lag (crm.ops.v1 1.2).
- Retention: inbox rows are deleted 90 days after they were received by the daily `crm_lifecycle` run
  (`crm.lifecycle_purge_expired`, crm-018; count `membership_inbox_purged`) and with the agency at offboarding.

## CIS account feed (AA-1028 R1 slice 2, root ADR 0019 d1 / d2)

Root contract `tenant.entitlements.v1` (CIS is the master of the tenant account, plans, subscriptions and
entitlements). `crm_entitlement_pull` (`app/cis/entitlement_feed.py`) makes `crm.tenant_entitlement` and the CIS part of
`crm.tenant.status` a read-only projection of it:

- Pages of `GET /tenant/v1/account-events` after the cursor in `crm.entitlement_feed_state` (crm-043; compare and set,
  as the membership feed), at most 5 a run; the feed only names the tenants that changed: each one the CRM holds
  (active or locked) is projected once per run from `GET /tenant/v1/tenants/{id}/account` (no snapshot call for a CIS
  tenant without a CRM agency). Once an hour (`last_reconcile_at`) every agency is projected, so an agency created
  after its tenant's last CIS event is covered; an agency whose snapshot fails is counted (`reconcile_failed_count`)
  and the pass goes on, with its own consecutive count (`reconcile_failures`, crm-043, never reset by the event pull):
  the next pass comes after 5, 10, 20, 40 minutes, then hourly, and 5 failing passes in a row log
  `crm_entitlement_feed_stuck part=reconcile`. Scope `cis.tenant/read`, the CRM's M2M client (`M2M_CLIENT`).
- The projection (`entitlement_feed_tx`, actor `platform:feed-tenant-account`, audit `aa_ops`): `plan` = the CIS crm
  subscription's plan while live (core / pro), else free (display only); `allowed_features` = the codes whose
  `crm.<code>` key is entitled; `member_limit` = `crm.seats` (kept when CIS gives none); `projected_at` = now;
  `tp_crm_leads` (crm-047, AA-1038) = both `tp.hosted` and `crm.core` entitled (CIS 390 routes the tenant's TripPlanner
  leads to the CRM then; `crm_tp_lead_pull` lists TripPlanner only for these agencies). An unchanged projection writes
  nothing. `in_service` false locks an active agency with `lock_source = cis_tenant`
  (crm-043 lets this actor name it); `in_service` true lifts only a `cis_tenant` lock. AA-969 d16: the snapshot's
  `is_pilot` (absent = no write: the agency keeps its current marker) is written to `crm.tenant.is_pilot` (crm-044, only this actor may; audit
  `tenant_pilot_marker`, `pilot_changed_count`). A refusal of one agency (CRMxx, integrity: a second pilot hits
  `tenant_one_pilot_uq`) is counted and the run goes on.
- **The pilot agency** (AA-969 part 2, root ADR 0019 d16, `app/lifecycle/pilot.py`): the one real agency allowed on Dev
  for at most 8 weeks. AA-initiated clean-up never touches it: `crm_lifecycle` skips its redaction, LLM call / inbox
  purge and auto-archive (its own trash purge still runs, on its own `trash_days`), the L1 test-agency delete refuses it
  (403 `pilot_agency`). What its own people ask for (erasure, offboarding, exports, imports) is never skipped.
  `crm_pilot_backup` exports it every night (above); `python -m app.portability.restore_drill` (inside the API
  container) opens the newest nightly export, checks both digests and every checksum, restores every table into
  temporary tables (`pg_temp`, dropped at commit) and prints per-table counts (manifest / restored / live / primary
  keys still live) and the documents the export left out per reason; `ok` when restored = manifest everywhere,
  `complete` when also no document was lost (past the size cap, missing, damaged): exit 0 complete, 3 restored but
  incomplete, 1 failed; `not_in_backup` lists what an export never holds (vendor credentials, agency keys, audit log,
  blind indexes); runbook: root
  `docs/ops/runbooks/pilot-tenant-restore-drill.md`. Kept as is (no pilot skip, reasons in
  `docs/implementation-notes/AA-969-l1-aa969-d16.md`): `crm_partner_accounts` (deletes only accounts no member or live
  invitation needs), `crm_job_retention` (job rows, not agency data), `crm_stage_date_rule`'s e-mail code expiry, the
  owner-driven `crm_offboard` / `crm_data_export` / `crm_client_import`.
- Enforcement (`app/entitlements.py`, `require_feature`): `ai_assist` (AI drafts, AA-1027 F3) and `whatsapp_sms`
  (connecting an account, the test message and a send; status, composer state, refresh and disconnect stay open) answer
  403 `feature_not_allowed` with a `denied` audit event. `whatsapp_sms` is also checked where no route is: a workflow
  `send_text` step is skipped `skipped_feature_not_allowed` (audited `denied`), and the dispatcher's text sender fails a
  queued SMS / WhatsApp `feature_not_allowed` at send time, so automations and texts queued before the feature was
  taken away send nothing. `stripe_connect`, `campaigns`, `custom_domain` and
  `insights_export` have no CRM feature yet; each gates its entry with the same call when it is built.
- Adoption guard: an agency never projected before (`projected_at` NULL, what the L1 console wrote) whose CIS account
  has no crm subscription in any status keeps its plan, seats and features (`awaiting_cis`, logged; CIS 355 created no
  crm subscription for the agencies that existed before). Once CIS holds a crm row the projection follows it (free with
  no feature after a cancel or offboard). CIS staff add the add-on with the CIS `tenant_provision` saga (attach mode).
  While an agency is not projected (`managed_by` ops: no CIS tenant, such as the CRM-seeded E2E agencies, or held
  here) AA still sets its allowed features on the L1 console (`PUT /entitlements-allowed`); once projected, 409
  `entitlements_managed_by_cis`. No revoke path yet once CIS manages an agency (no saga cancel, no override endpoint):
  slice 3. `/crm/health/ready` reports `entitlement_feed` in one word
  (never fails readiness).

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
- **Kept address** (AA-1035, crm-045, crm.ops.v1 1.12): every issue with an address also seals it under a one-off data
  key of the CRM KMS key (context `{tenant_id: the CIS tenant, purpose: mail}`, the key stored wrapped in the row:
  `email_kept*`). `{cis_invitation_id, locale, keep_address: true}` (no `email`) re-issues to that address, so nobody
  types it again and a support member (who may only send invitations again) can never redirect one; a row that kept
  none (issued before crm-045 or without a key provider) answers 409 `address_not_kept`. The claim clears it, and so
  must a revoke (the crm-045 guard refuses a revoke that keeps it); an expiry keeps it, since an expired invitation is
  re-issued (the usual send again).
- **Desk callers** (AA-1035, `cis-customer_success` / `cis-support`): they hold the WORKFORCE token CIS relays, so
  they can call this route without CIS's owner-tier check. The CRM therefore asks CIS itself
  (`studio.check_desk_invitation`, `tenant.membership.v1` getTenantMembers of the path tenant, M2M) and delivers only an
  `invited` membership of that tenant with the role `viewer` or `editor`: 403 `owner_invitation_not_allowed` for an
  owner invitation (with a new address or the kept one), 403 `invitation_not_invited` for an id CIS does not hold open
  on that tenant (an invitation of another tenant included), 503 `cis_unavailable` when CIS cannot answer.
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
