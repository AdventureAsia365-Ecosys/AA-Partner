# AA-1111 / AA-1166 — CRM hardening slice (S294, lane l22)

Plain hardening after the S294 self-assessment pass. Shipped as two PRs: this non-sign-in slice now, and a sign-in
slice (cookie prefixes, self-service password change) as a draft that waits for the sign-in change window.

## Decisions

- **JSON bodies name their charset** in the existing `SecurityHeadersMiddleware` (one place for every response,
  including 404s and refusals): a bare `application/json` becomes `application/json; charset=utf-8`. Other media types
  and a content type that already carries parameters stay as the route set them.
- **One reference page** `docs/operations/security-controls.md` (`SEC-01-crm-controls`, a sub-page of the root
  security baseline): input checks per data item, the sign-in protections, the capability tokens in link URLs.
  Status `draft` until the tech lead reads it.

## Changed

- `api/app/observability/security_headers.py`: `with_json_charset()`, `JSON_CONTENT_TYPE`, applied in the middleware.
- `CONTEXT.md` stack row points at the new page.

## Tradeoffs

- Normalising in the middleware instead of a custom default response class: also covers responses built by
  exception handlers and Starlette, and needs no change in any route.
- No new setting: nothing here is a threshold.

## Should know

- Every web consumer of API JSON tests the content type with `startsWith("application/json")`
  (`web/lib/bff.ts`), so the added parameter changes nothing for the web.
- The unhandled 500 is plain text and is not touched.
