# AA-1040 — AI mark headers through the Content studio BFF (AA-Partner web)

Lane l6-aa1040-export-headers, S272. Follows `AA-1040.md` (S266: studio badges, CRM draft mark) and AA-CIS-App
`docs/implementation-notes/AA-1040-marking.md` (the CIS export route sets `X-AI-Generated` / `X-AI-Generator` /
`X-AI-Model` on every piece download; the HTML body carries `data-ai-*` and `<meta name="ai-*">`; the T2 DOCX carries
the mark in its core properties).

## Decisions

- **Where the headers were lost:** every studio download goes through the tenant BFF
  `web/app/api/tenant/[...path]/route.ts`, which answers with `streamUpstream()` (`lib/proxyResponse.ts`): only
  `Content-Type` comes back. So on the agency host the browser never saw `X-AI-*` on an export, and never saw the
  DOCX `Content-Disposition` either (the Catalog tab fell back to `tour.docx` for every version). The bodies were
  never touched (streamed as bytes): the HTML `data-ai-*` / `<meta name="ai-*">` and the DOCX core properties reached
  the browser intact; the client only swaps the HTML `<title>` (`withDocumentTitle`), which leaves the marks alone.
- **Fix in a Partner-only module, not in the vendored copies.** `lib/proxyResponse.ts` and `lib/upstreamHeaders.ts`
  are byte copies of AA-CIS-App `frontend/lib/*` (CI "Vendored CIS copies"); changing them would need a CIS PR first
  and would change the CRM and staff BFFs too. New `lib/studio/downloadHeaders.ts`; the tenant route sets its result
  on the streamed response.
- **Allow-list, values checked, never copied blindly** (same rule as the CRM BFF's `attachmentName`, AA-1030):
  - `X-AI-Generated`: exactly `true` or `false`, else nothing of the mark is passed;
  - `X-AI-Generator`, `X-AI-Model`: printable ASCII, 1 to 200 characters (CIS sends a plain name and a model id),
    passed only together with a valid `X-AI-Generated`;
  - `Content-Disposition`: rebuilt as `attachment; filename="<name>"` from a checked name (letters, digits, `.`,
    `_`, `-`, ending `.docx`, `.html` or `.txt`); CIS's DOCX header is unquoted (`filename=tour_<id>_v<n>.docx`),
    both forms are read.
  Any other upstream header (Set-Cookie, CORS, caching, encodings) is still dropped.
- **The browser reads them same-origin** (`fetch("/api/tenant/...")` on the agency host), so no CORS
  `Access-Control-Expose-Headers` is needed: the browser never calls CIS directly in the merged app (the BFF fetches
  CIS server side). CIS's own CORS config is irrelevant to this path.
- **Catalog tab file name:** `CatalogTab.exportDocx` read the name with `/filename=([^;]+)/`, which would keep the
  quotes of the rebuilt header; it now uses the same `dispositionFileName()` as the BFF.
- **Playwright check** `e2e/smoke-portal/aa1040-export-marks.spec.ts` (portal smoke, runs after Deploy Web Dev):
  signed in as the e2e studio user, exports one ready blog piece as an HTML document and as a fragment, and one other
  ready piece as text, and asserts the `X-AI-*` headers plus the in-body marks; and one T2 version's DOCX:
  `Content-Disposition` with a `.docx` name. Skips without the sign-in values, annotates (not fails) when the e2e
  tenant has no ready piece or version. Each export writes one CIS audit row (`content_exported` /
  `tour_version_exported`), the same as a person clicking Export; no LLM call, no publish.

## Changed

- New `web/lib/studio/downloadHeaders.ts` (`studioDownloadHeaders`, `dispositionFileName`).
- `web/app/api/tenant/[...path]/route.ts`: the streamed answer carries the checked headers.
- `web/app/(studio)/portal/_components/CatalogTab.tsx`: DOCX file name through `dispositionFileName`.
- Tests: `web/tests/studio/downloadHeaders.test.ts` (unit: allow-list, values, file names, the route end to end
  with a fake CIS); `e2e/smoke-portal/aa1040-export-marks.spec.ts`.

## Tradeoffs

- Passing the mark headers on every tenant path (not only `/export` and `/export-docx`): simpler and no path list to
  keep in step with CIS; JSON reads do not send `X-AI-*`, and `Content-Disposition` passes only for a checked file
  name of the three download types.
- A text export saved to disk carries no mark (a `.txt` file has nowhere to keep one; the headers reach the page but
  a blob download does not store them). This is the CIS design (headers for text); unchanged here.

## Should know

- Remaining surfaces checked from code (S272), nothing redone: catalog.http.v1 1.3 `Tour.ai` (CIS
  `services/catalog_http`) and the TripPlanner browse Lambda `catalog_ai_mark`; Booking supply text `content.ai` /
  `supply.ai` (CIS 358). Still without a mark: Marketplace rows (tour name only, as the register says). In this repo
  the CRM's AA tour cache (`crm.aa_catalog_tour`, crm-017) keeps the whole catalog.http.v1 body (so `Tour.ai` too), but
  agencies read only its title, country, days and state (`app/catalog/store.py` `search` / `tour`): no AI body text
  reaches a CRM page, so no mark is needed there. The AA prep packs and starter templates (`crm.aa_content`) are
  written by AA staff in CIS, not by a model.
- The T2 DOCX gets no `X-AI-*` headers from CIS (its mark is the file's core properties, which survive any proxy);
  adding the headers is a CIS change, listed as a follow-up.
- The CRM BFF (`/bff/*`) and the staff BFF are unchanged: no CRM download is AI output (CSV reports, portability ZIP,
  documents); the CRM AI draft is JSON with `ai` and a DOM mark.
