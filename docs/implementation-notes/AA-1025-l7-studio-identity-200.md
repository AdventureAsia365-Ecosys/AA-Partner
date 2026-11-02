# AA-1025 (S264 lane l7-studio-identity-200): the studio's CRM sign-in line without console errors

Hotfix after Partner #191 (lane l3-ui-fixes, `AA-1025-l3-ui-fixes.md`). The Content studio asked the CRM web's
`GET /bff/me` for the "Signed in to AA Partner ..." line. For a person whose CRM session has no agency picked, the BFF
catch-all answers 409 `no_agency_selected` (401 without a session). The browser logs every non-2xx fetch as a console
error, so the post-deploy portal UI smoke "portal: Account settings (/portal/account/settings)" failed on console
errors (Deploy Portal Dev run 41370456920).

## Decisions

- **A new literal route, `/bff/me` unchanged.** `web/app/bff/identity/route.ts` sits beside `app/bff/[...path]`; a
  literal segment wins over the catch-all in Next (checked on the built standalone server: `/bff/identity` 200,
  `/bff/me` still 401). The CRM pages rely on `/bff/me` 401 (reload through `/auth/refresh`) and 409, so they stay.
- **Always 200 for a GET**, the answer says what is known: `{kind: "signed_out"}` (no session or an expired access
  token), `{kind: "no_agency"}`, `{kind: "agency", display_name, member_role}`, `{kind: "unknown"}` (API non-200,
  unreachable, timeout, redirect, not JSON, not an object).
- **Same checks as the catch-all, in its order.** Host other than the partner host -> 404 (the proxy's check
  repeated); session read with `readSession` / `accessValid`; the token and agency come only from the sealed session
  (browser `X-AA-Tenant-Id` / `Authorization` ignored); upstream `GET /crm/v1/me` built with `upstreamHeaders`, and the
  agency header and timeout come from the allow-list entry of `me` (`matchBff("GET", "me")`), so the allow-list stays
  the one place that says what the web may call.
- **Only two fields leave the web.** `web/lib/crmIdentity.ts` `pickIdentity` keeps `display_name` and `member_role`
  (trimmed, at most 200 characters, null when not a non-empty string); tenant and member ids, locale and anything the
  API adds later are dropped. It lives in `lib/` because a Next route file may export only handlers and config.
- **Other methods: explicit 405 with `Allow: GET`**, after the host check and the shared write guard
  (`refuseForeignWrite`, so a cross-site write is 403 as on every write route; `tests/writeGuard.test.ts` requires the
  guard on any route exporting a write method). Without explicit handlers Next would 405 by itself, but then the
  answer could not be unit tested.
- **Portal reads the new shape, tolerant.** `portal/lib/crmIdentity.ts` calls `CRM_IDENTITY_PATH = "/bff/identity"`;
  `readCrmIdentity` maps `no_agency` and `agency` (with a non-empty name) and treats everything else as "show nothing":
  `signed_out`, `unknown`, an unknown `kind`, a non-200 and the old `/bff/me` shapes (no `kind`). The rendered
  sentences are unchanged.
- **Role lookup hardened on the way:** `Object.hasOwn(ROLE_LABEL, role)` instead of `role in ROLE_LABEL`, so a role
  string like `constructor` cannot pick an inherited property.

## Changed

- web: `app/bff/identity/route.ts` (new), `lib/crmIdentity.ts` (new), `tests/studioIdentity.test.ts` (new: forward
  with the session's token and agency, no echo of other `/me` fields, `signed_out` incl. expired token and a garbage
  cookie, `no_agency`, seven upstream failures -> `unknown`, foreign host 404, 405 / 403 / 404 for write methods,
  `/bff/me` still 401 / 409, proxy matcher + gate pass `/bff/identity` without a session, `pickIdentity`).
- CI: the web "Standalone server smoke" also checks `curl /bff/identity` == `{"kind": "signed_out"}`.
- portal: `lib/crmIdentity.ts` (path + shape), `lib/crmIdentity.test.ts` (new shape, old shapes read as nothing,
  inherited role names).
- `CONTEXT.md`: BFF section names the literal route; the studio sign-in paragraph points at `/bff/identity`.

## Tradeoffs

- An expired CRM access token reads as `signed_out` (the studio shows nothing), although the refresh cookie could renew
  it. The studio never refreshes the CRM session itself; that is the CRM page's job (`/auth/refresh`), as before.
- Upstream errors become `unknown` and are not logged by this route; the API logs its own failures. Adding a web log
  line per studio page view was not worth the noise.
- `/bff/me` and `/bff/identity` both exist; the duplication is a few lines and keeps the CRM pages' contract intact.

## Should know

- Routing on Dev: `portal.<zone>` CloudFront (CachingDisabled) -> ALB; `/bff/identity` matches no studio rule
  (`/portal*`, `/api/*`, `/tenant-login*`, `/terms`, `/privacy`, `/assets/*`) and goes to the CRM web
  (`crm_agency_default`, AA-CIS-Infra `crm_agency_edge.tf`, read only). `web/proxy.ts` passes every `/bff/*` path through
  on the partner host; the matcher excludes only `/bff/mail/imports`. No Infra change.
- Deploy order: the web first (until it is out, `/bff/identity` on Dev is the catch-all's 404 `not_found`, which the
  new portal reads as "show nothing" but which is still a console error), then re-run Deploy Portal Dev.
