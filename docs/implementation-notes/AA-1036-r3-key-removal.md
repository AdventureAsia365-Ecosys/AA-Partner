# AA-1036 R3 slice 3 — no tenant API keys (AA-Partner part)

Root ADR 0019 d6: "All tenant API-key code and data go too." The full caller inventory (every repo, file:line) and the
CIS side are in AA-CIS-App `docs/implementation-notes/AA-1036-r3-key-removal.md`. This note covers the agency web.

## Decisions

- **The studio's API page goes** (`/portal/account/api`: `ApiTab`, `ApiPlayground`, `api-playground/*`). Its whole
  content was the API-key flow (`POST /auth/tenant-login` with `api_key`, then a Bearer JWT) and a playground that
  documented `X-API-Key`. With the keys gone there is no agency machine path to document until R8, so the page is
  removed instead of rewritten. `/portal/api` and `/portal/account/api` answer 308 to `/portal/account/settings`
  (`next.config.ts` `STUDIO_REDIRECTS`), and the sidebar / palette entry "API" is gone.
- **The playground BFF goes too** (`app/api/playground/proxy`): it served only the playground UI.
- **Integrations loses the API key card** (`ApiKeyCard`, CIS `GET /v1/account/api-key`, removed in the CIS PR), and
  Account settings loses its "API access" card and the "new API key" wording.
- **Smoke and phase-2 runs sign in with the partner pool only**: the local `E2E_TENANT_API_KEY` path of
  `e2e/smoke-portal/auth.setup.ts` and the optional phase-2 "E check" (`E2EP2TENANTKEY`) are removed (CIS answers 410).
  The `/tenant-login` 308 check now runs on every target.

## Changed

- `web/`: pages and components above, `lib/studio/account.ts` (`ApiKeyFacts`, `keyUseText` removed), `portalNav.tsx`,
  `next.config.ts`, the billing note copy ("API calls are requests made outside the Content studio"),
  `eslint-suppressions.json` pruned (entries of the deleted files).
- `e2e/`: `auth.setup.ts`, `portal-helpers.ts`, `portal-smoke.spec.ts`, `aa761-account-pages.spec.ts` (Integrations
  shows no key card), `aa885-portal-host.spec.ts`, `aa911-portal-cross-site.spec.ts`, `phase2/*`.
- `api/tests/unit/test_studio_guards.py`: new `test_the_studio_has_no_tenant_api_key_code` (no `/auth/tenant-login`,
  `/v1/account/api-key`, `X-API-Key`, key card or API page in the studio code; comments may keep history), the 308
  map adds `/portal/account/api`, the write-route list drops the playground BFF.
- `CONTEXT.md`: "One agency app" gets the "No tenant API keys" bullet; history sections point at it.

## Tradeoffs

- Rewriting the API page for a future machine API would document something that does not exist; R8 builds it with
  its own credentials, so removing it now is cheaper than keeping a page that tells agencies to do something that
  answers 410.

## Should know

- Ship this PR before (or with) the CIS PR: the deployed studio must not call `GET /v1/account/api-key` after CIS
  removes it (the card would show an error until this deploy).
- The repo secret `E2E_TENANT_API_KEY` was already orphan-deleted at the P2b clean-up (root secrets register); nothing
  here reads it any more.
