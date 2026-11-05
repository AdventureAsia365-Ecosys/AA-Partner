# AA-1036 R3 — the CRM owner's add-on request (lane crm-addon, S269)

Found live on Dev (S269): an agency owner with the CRM but no Content studio membership clicked the CRM sidebar entry
"Trip planner: request add-on" and landed on the studio's `/portal-access/none` ("No Content studio access"). The entry
was shown because the CRM keeps the studio link when CIS cannot answer the studio-access check, and it pointed at the
studio's Add-ons page, which only a studio member can open. A CRM-only agency could not ask for an add-on at all,
although root ADR 0019 d7 says add-ons switch on by entitlement or request.

## Decisions

- **A CRM-side page, one store.** Settings › Add-ons (`/settings/add-ons`) in the CRM. The CRM API relays to CIS over a
  new versioned contract `tenant.addons.v1` (root `docs/contracts/`, byte copy here pinned by sha256): `GET
  /tenant/v1/tenants/{id}/add-ons`, `POST /tenant/v1/tenants/{id}/add-on-requests`. CIS serves them with its unchanged
  studio service, so `shared.tenant_addon_request` stays the only store and Tenant 360 the only decision path; the
  studio's Account › Add-ons shows the CRM owner's request too.
- **No existing relay fit.** `tenant.entitlements.v1` is read-only (`cis.tenant/read`); `crm.ops.v1` goes the other
  way (CIS staff -> CRM). The CRM has no CIS tenant token (CRM sessions are partner-pool tokens of the web client;
  CIS `/v1` accepts the studio client only). So: M2M, its own scope `cis.tenant/addons` (AA-CIS-Infra
  `identity_m2m.tf`; the CRM client takes every scope of `cis.tenant`).
- **Owner only.** Reading uses `PLAN_VIEW` (already owner only: Team › plan); asking a new action `ADDON_REQUEST`
  (owner; pinned in `test_permissions.py`). The CRM decides the role (root ADR 0010 d5); CIS records the requester as
  `crm-member:<member id>`, never a name or an e-mail. The sidebar's Trip planner entry now goes to the CRM page and is
  shown to the owner only; the "Apps" group shows when there is a studio link or the entry.
- **No connection held across the CIS call.** `tenant_tx(action=…)` checks membership and role (a refusal is audited
  as usual) and closes; the CIS call runs without a fair-share slot; a second short `tenant_tx()` writes the audit:
  `addon_request` allowed (object = CIS request id, reason `<product>_created` / `<product>_open`) or denied
  (reason `cis_<CIS reason>`). The AI draft's `long=True` slot was the alternative; this keeps the 3-connection pool
  free during a slow CIS.
- **Contract shape, closed.** `app/cis/addons.py` copies the CIS answer field by field and checks every value; a field
  CIS adds later is dropped, a body off the contract is 503 `addons_unavailable` (`cis_contract` in the log).
- The CIS client config is the existing `app.state.studio_activation` (CIS origin + M2M token provider), as
  `app/cis/invitation_lookup.py` does: same origin, same client.

## Changed (vs. the lane brief)

- The brief offered "CIS POST /v1/account/add-on-requests semantics"; the CRM cannot call `/v1` (studio token only),
  hence the new `tenant.addons.v1` routes in CIS (AA-CIS-App PR) and the scope in AA-CIS-Infra.
- Error mapping to the web: CIS 409 / 429 keep CIS's reason as `detail` (`already_subscribed`,
  `tenant_not_in_service`, `too_many_requests` with `Retry-After`), 400 becomes 422 with the reason, 404
  `tenant_not_found` becomes 404 `addons_no_account`; anything else (CIS down, 403 scope not granted yet, 404 route not
  served yet) is 503 `addons_unavailable`.

## Tradeoffs

- If the second `tenant_tx()` fails after CIS stored a new request, the request stands without a CRM audit row (CIS's
  own audit names the CRM member) and the page shows it as requested after a reload. Rare (the agency would have to be
  locked between the two blocks).
- The page makes one CIS call per load (owner only, a settings page); the sidebar label stays static ("Trip planner:
  request add-on") to avoid a CIS call on every CRM page.

## Should know

- Ship order: root contract -> AA-CIS-Infra scope (apply twice: resource server, then the
  client; re-plan until "No changes") -> AA-CIS-App routes deployed -> this PR. Before CIS serves the routes the page
  says the add-ons cannot be shown (503), nothing else breaks.
- No migration, no new table: the CRM keeps nothing of the request.
- Tests: `api/tests/unit/test_aa1036_crm_addons.py` (contract pin, parsing, outcomes, routes, audit),
  `api/tests/integration/test_aa1036_crm_addons.py` (live app on PostgreSQL, fake CIS, audit rows),
  `web/tests/addOns.test.ts`, `web/tests/crmNavGroups.test.ts` (CRM-only owner keeps the entry; agent without studio
  has no group), `web/tests/studio/merge.test.ts` (`crmApps` / `plannerHref`).
