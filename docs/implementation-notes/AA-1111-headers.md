# AA-1111 — security headers slice (S284, lane l5-aa1111-headers)

Source: the S283 ZAP baseline triage (root `docs/audits/2026-11-17-S283-access-review/zap-baseline.md`). Same slice
as the CIS and Booking admins (their `docs/implementation-notes/AA-1111-headers.md`).

## Decisions

- **Full CSP with a nonce on the CRM web** (`web/lib/securityHeaders.ts`, used by `web/proxy.ts`). The proxy already set
  the CSP per request (form-action names the host's managed-login domain, frame-ancestors the inquiry form's origins);
  `contentSecurityPolicy(cognitoDomain, ancestors, staffCognitoDomain, nonce)` keeps that signature and now builds the
  whole policy: `default-src 'self'`, `script-src 'self' 'nonce-…' 'strict-dynamic'`, explicit fetch directives.
- **One nonce per request** through a `WeakMap<NextRequest, string>` (`nonceFor`), so the gate, the studio step and the
  form step of one request put the same nonce on the response, the request CSP (Next's scripts) and `x-nonce`
  (`app/layout.tsx`). A browser-sent `x-nonce` is dropped with the other internal headers.
- **Every proxy response gets a CSP** (`withCsp` in `proxy()`): the plain 404 of an unknown host or path and the
  sign-in redirects had none.
- **frame-ancestors first** in the header: the CI standalone smoke greps `content-security-policy: frame-ancestors
  'none'` (left as is, no workflow change).
- **style-src keeps `'unsafe-inline'`** (kit inline styles); **img-src / media-src** allow any https source (photos,
  agency logos, academy videos on presigned S3 URLs).
- **HSTS stays two years with sub-domains, no preload**.

## Changed

- `next.config.ts` takes the static list from `lib/securityHeaders.ts` (+ COOP same-origin, `payment=()`, `usb=()`) and
  sends the no-nonce policy on the paths outside the proxy matcher.

## Tradeoffs

- No settings row for the CSP (the decision said not to invent one).
- No COEP: it would block cross-origin images without CORP (tour photos).

## Should know

- Local check (standalone server, Chromium): `/signed-out` and the legal pages render with every script carrying the
  nonce and no CSP violation; an injected inline handler is refused; the CI smoke's CSP grep still matches.
- `tests/securityHeaders.test.ts` (builder, nonce per request, forged x-nonce, 404 / redirect CSP, next.config rules,
  layout nonce); `e2e/smoke-portal/aa885-portal-host.spec.ts` checks the deployed host (legal pages, `/robots.txt`, an
  unknown path).

## S294 (lane l16-sec-headers, ZAP re-run of 25/11)

- **Decisions:** the CRM web already sends the full set (HSTS, Permissions-Policy, COOP, nonce CSP, no
  `X-Powered-By`) on `/`, `/robots.txt` and a 404 (live `curl`); the one gap was the CRM API: `/crm/*` answered without
  HSTS and `nosniff`. `app/observability/security_headers.py` is the Booking API middleware (S284) with the same two
  headers and the same rule (a header the endpoint set wins). Registered between the request id and the body ceiling,
  so a 413 carries them; the unhandled 500 gets them from `unhandled_error_handler`. uvicorn already runs with
  `--no-server-header`.
- **Changed:** `api/tests/unit/test_aa1111_api_security_headers.py`; the portal smoke checks `/crm/health` on the agency
  host (Fargate target only: the local web has no `/crm/*`).
- **Tradeoffs:** no `X-Frame-Options` / CSP on the JSON API (same choice as CIS and Booking: no HTML page to frame);
  no settings row (header values are fixed policy, not a threshold).
- **Should know:** the ZAP "Permissions-Policy not set" on the portal root in the S294 run did not reproduce live (the
  root 307 carries it with and without a `User-Agent`). The smoke check of `/crm/health` runs on the next web deploy,
  after this API deploy.
