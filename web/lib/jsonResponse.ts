// web/lib/jsonResponse.ts
// S294 hardening: every JSON answer of the CRM web (route handlers, the BFF refusals, the studio routes) names its
// charset ("application/json; charset=utf-8"), so no client has to guess the encoding. The CRM API does the same in
// api/app/observability/security_headers.py; AA-TripPlanner-Web has the same helper (frontend/lib/jsonResponse.ts).
// tests/jsonResponse.test.ts checks that every NextResponse.json / Response.json in app/ and lib/ goes through jsonInit.
// Pure (no Next import), so any test can load it.

export const JSON_CONTENT_TYPE = "application/json; charset=utf-8";

/** The ResponseInit of a JSON answer: the caller's status and headers, with the JSON content type and its charset. */
export function jsonInit(init: ResponseInit = {}): ResponseInit {
  const headers = new Headers(init.headers);
  headers.set("content-type", JSON_CONTENT_TYPE);
  return { ...init, headers };
}
