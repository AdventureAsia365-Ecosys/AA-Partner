# AA-979 P2b: Content studio invitations in the CRM API

Session S252, agent Claude Code. Branch `feat/aa-979-studio-invitations-api` (stacked on
`feat/aa-979-crm-studio-invitation`, crm-020). Design: AA-Ecosys `.tmp-session/s252/aa979/design.md` §2.3, critic C6 /
S12; orchestrator decisions Q4 (staff invites via the CIS relay), Q10 (separate `crm.studio_invitation`).

## Decisions

- **One join link for both kinds of invitation.** The claim and accept routes try the CRM invitation first and, on its
  404 `invitation_not_found`, the studio invitation (`app/routers/invitations.py`). The web's join pages need no new
  route; the accept answer of a studio invitation carries `kind: "studio"` so the join page sends the person to the
  Content studio instead of an agency.
- **Q4 — writers of the relay route**: any OPS caller (`cis-admin` included). The CIS route in front of it already
  requires `tenant.write`, CIS decides the role, and the call grants nothing in the CRM. `test_route_guard` lists it as
  the one ops write a `cis-admin`-only token may make; `test_ops_contract` exercises it (no agency audit expected).
- **No CRM role from CIS.** The design's optional `crm_role` was dropped (CIS must never grant a CRM role, ADR 0010
  §7); a person who also needs a CRM role gets the CRM's own invitation.
- **The sealed address** (`seal_email` / `open_email`): HKDF-SHA256(token, salt, info = CIS invitation id) ->
  AES-256-GCM with the CIS invitation id as AAD. No agency DEK exists for a studio-only tenant; the token only lives
  in the mail, so a database dump cannot open the address.
- **Activation outbox**: accept marks `accepted` and queues `crm_studio_activation` in the same transaction, then tries
  once at once (fast path). The CIS call never runs inside a transaction; the result is written under a fresh lock
  and only while the row is still `accepted` (a parallel attempt that won is left alone). 401 drops the cached M2M
  token; 403 (scope not granted yet) retries rather than fails, so the order Infra / CIS / CRM is forgiving.
- **M2M scope** `cis.tenant/activate` is asked for alone (`app/cis/activation.py`), like every M2M call here (critic S12).
- **Contexts** `studio_platform_tx()` / `studio_invitation_tx()` in `app/tenancy.py`, opened only by
  `app/members/studio.py` (`test_code_rules`); `StudioContext` has no tenant, no vault, no audit queue.

## Changed

- Design §2.3 status list gained `accepted` (between claim and CIS activation) for the retry.
- `crm.ops.v1` gains the route (contract 1.3 in the root repo, same session) and the response key `reissued`.

## Tradeoffs

- No agency audit for studio invitations: a CIS tenant may have no CRM agency. Log lines (ids, codes) and the row
  itself are the record; the CIS side writes `tenant.member_invited` / `tenant.member_activated` audit rows.
- The fast path costs one CIS round trip inside the accept request (10 s timeout at worst); worth it so most people
  see "ready" instead of "being set up".

## Should know

- Merge only after crm-020 is applied on Dev (DB Migrate) and the schema-dump PR merged: this PR adds `crm-020` to
  `REQUIRED_SCHEMA_VERSIONS`, so `/crm/health/ready` is 503 until then (the deploy smoke would fail).
- Needs CIS PR (`/tenant/membership/v1/invitations/{id}/activate`) deployed and Infra PR-1 applied (scope on the CRM
  client) for activations to succeed; until then accepted rows wait and the job retries (12 attempts, backoff up to
  15 min each: roughly two hours), then fails. The person re-opening the accept page drives one more job (at most one
  per UTC hour) and one more attempt, so nobody is stuck; still, order the deploys so CIS and Infra land first
  (runbook).
- Verified locally: unit suite 1703 passed (coverage 93 %, `app/members/studio.py` 97 %, `app/cis/activation.py`
  100 %), integration suite 464 passed on PostgreSQL 16 with the RDS role shape.
