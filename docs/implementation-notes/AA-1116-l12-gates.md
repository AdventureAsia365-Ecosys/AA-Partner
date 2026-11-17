# AA-1116 — the remaining CRM feature gates (S283 lane l12, Partner part)

S282 add-on audit G10: of the six CRM feature codes on the CIS plans (`crm.ai_assist`, `crm.whatsapp_sms`,
`crm.campaigns`, `crm.stripe_connect`, `crm.custom_domain`, `crm.insights_export`), three were checked in this repo
(AI drafts, SMS / WhatsApp, campaigns). This lane gates the two that have a CRM feature today and makes the gap
visible for the one that does not. The pricing part of this card is in `AA-1116.md`.

## Decisions

- **Error code `feature_not_allowed`, not a new `entitlement_missing`.** The lane brief named `entitlement_missing`;
  no app of the ecosystem uses that code, while the CRM's shared refusal for a feature off the plan has been
  `403 {"detail": "feature_not_allowed", "message": ...}` since AA-1027 (AI drafts, SMS / WhatsApp, campaigns), and the
  web already maps it (aiDraft, aiExtras, campaigns, messaging). A second code for the same refusal would split the
  web's handling; the new gates reuse `app.entitlements.require_feature` (403 + a committed `denied` audit event).
- **`insights_export` = the Reports CSV export.** The root catalogue ties it to CRM-F020 Reports ("funnel, money,
  what clients owe, the team and CSV export"; conditions `crm.insights_export`), on plan crm.pro only. The gate sits in
  `app.reports.export.export` (every section, the AA-1068 profit cube too) before any read, and before the money role
  check, so an assistant on a plan without it reads the plan refusal the page explains. The Reports page itself (and
  the AA insights opt-out note) stays open on every plan: only the download is gated.
- **`stripe_connect` gates connecting Stripe only.** The CIS key reads "CRM card payments on the agency's Stripe" and the
  catalogue INT-071 names it for Stripe. Stripe is still paused (root ADR 0019 b14), so on the plan the answer stays
  409 `provider_not_available`; off the plan it is 403 first. The VN gateway (AA-1072, the agency's own VNPay merchant)
  is not gated by it: putting a Stripe-named key on VNPay would be a new product rule (follow-up for the tech lead).
- **`custom_domain` is not built** (client pages on the agency's own domain): nothing to gate. `entitlements.NOT_BUILT`
  names it, and `tests/unit/test_aa1116_feature_gates.py` fails when a feature code is neither used in `app/` nor in
  that list, or when a NOT_BUILT code shows up in `app/` (the list must shrink when the feature is built).
- **Web: locked, not hidden.** Both CSV download controls (Reports "Export CSV", the profit cube "Download CSV") show
  a disabled button with a lock icon and "CSV export is not on your agency's plan. Ask Adventure Asia to add it."
  Settings > Payments shows Stripe "Not on your plan" with the ask. The agency learns the feature exists and whom to
  ask; no plan name is written in the copy (plan contents are CIS data the administrator edits).
- **An older API counts as allowed** (`export_allowed` / `on_plan` absent): the deploy order web-before-API never locks
  a feature the plan has; the API refuses anyway.

## Changed

- `api/app/entitlements.py`: `NOT_BUILT`.
- `api/app/reports/export.py`, `service.py`: the gate, `EXPORT_FEATURE`, `export_allowed` in the summary.
- `api/app/payments/providers.py`, `service.py`: `PLAN_FEATURES`, `available(not_on_plan)` with `on_plan`, the gate in
  `connect`.
- `web`: `ExportMenu` (+ `ExportLocked`), `ProfitExport`, `ProfitSection`, the Reports page, `PaymentMethodsForm`,
  `lib/reports.ts` (`exportLocked`), `lib/invoices.ts` (`providerBadge`), three EN + VI messages.
- Tests: `api/tests/unit/test_aa1116_feature_gates.py`, the reports and payments unit tests, the reports and
  finance integration tests (the agency gets `insights_export`; a new test for an agency without it);
  `web/tests/aa1116Gates.test.ts`, `web/tests/reportsPage.test.ts`.

## Tradeoffs

- The gate is read once per download and once per summary (one indexed row per agency): no cache, so a plan change in
  CIS reaches the agency at the next `crm_entitlement_pull` slot, like the other gates.
- The denied download writes an audit event per click; the access log shows the agency tried, which is also the
  upgrade signal.

## Should know

- No migration, no new setting (no threshold was added).
- Dev agencies on crm.core lose the CSV export after the deploy (crm.core has no `crm.insights_export`). The CIS plan
  is the place to change that (Platform › Tenants › Plans, or a per-tenant override).
- Live check: as the owner of an agency on crm.core, Reports shows the locked export and
  `GET /crm/v1/reports/export?section=funnel` answers 403 `feature_not_allowed`; Settings > Payments shows Stripe "Not on
  your plan". On a crm.pro agency the download works.
