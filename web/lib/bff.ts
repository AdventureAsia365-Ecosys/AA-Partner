// web/lib/bff.ts
// AA-935 — the BFF allow-list (card CRM-07 "BFF allow-list"). The browser never talks to the CRM API: it calls
// /bff/<path> on its own origin, and app/bff/[...path]/route.ts forwards ONLY the (method, path) pairs listed here
// to /crm/v1/<path>, with the session's access token and the session's agency (X-AA-Tenant-Id taken from the
// sealed session, never from a client header). Anything else is 404 before a token is read.
//
// Adding an API route to the browser = adding one line here (reviewed with the route). Server components use the
// same list (lib/crmApi.ts), so there is one place that says what the web may call.

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface BffRoute {
  method: Method;
  /** Matched against the encoded sub-path after /bff/ (lib/bffPath.ts proxyPath). Anchored. */
  pattern: RegExp;
  /** The call needs the agency picked in the session (sent as X-AA-Tenant-Id). */
  tenant: boolean;
  /** Card that added it (review trail). */
  card: string;
  /** Largest request body the BFF forwards for this route (default 256 KiB, app/bff/[...path]/route.ts). */
  maxBody?: number;
  /** AA-945: the body is raw bytes (a file), forwarded unchanged as application/octet-stream, not JSON text. */
  raw?: true;
  /** Upstream timeout for this route (default 15 s, app/bff/[...path]/route.ts). */
  timeoutMs?: number;
}

/** AA-937: a document upload is base64 JSON of at most 7 MiB (~9.4 MiB), under the proxy's 10 MiB body clone limit. */
export const DOCUMENT_BODY_BYTES = 10 * 1024 * 1024;

/**
 * AA-945: the mail history upload (mbox / CSV) is the raw file of at most 25 MiB (the API's
 * history_import.MAX_BYTES). Its path is left out of the proxy matcher (proxy.ts), so Next does not clone the body
 * (the proxy's 10 MiB clone limit would cut a larger file short without an error).
 */
export const MAIL_IMPORT_BODY_BYTES = 25 * 1024 * 1024;
/** AA-962: a client import file (CSV / .xlsx) is the raw file of at most 5 MiB (api/app/imports/formats.py MAX_BYTES),
 * under the proxy's 10 MiB clone limit, so its path stays inside the proxy matcher. */
export const CLIENT_IMPORT_BODY_BYTES = 5 * 1024 * 1024;
/** Raw uploads (<= 25 MiB each) one web process reads / forwards at once (the task has 512 MB); more -> 503. */
export const MAX_RAW_IN_FLIGHT = 2;
/**
 * The proxy matcher leaves out every path under this prefix (proxy.ts); the BFF route checks the host itself. The raw
 * route's upstream timeout (50 s) stays under the CloudFront origin read timeout (60 s), so the browser gets the BFF's
 * own 504 `api_timeout`, not CloudFront's.
 */
export const UNPROXIED_BFF_PREFIX = "/bff/mail/imports";

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

export const BFF_ROUTES: readonly BffRoute[] = [
  { method: "GET", pattern: /^me$/, tenant: true, card: "AA-933/AA-935" },
  { method: "GET", pattern: /^memberships$/, tenant: false, card: "AA-935" },
  // AA-936 (CRM-08 Slice 1): board, trip workspace, moves, assignment, notes, tasks, contact reveal, forms.
  { method: "GET", pattern: /^pipeline$/, tenant: true, card: "AA-936" },
  { method: "GET", pattern: new RegExp(`^trips/${UUID}$`), tenant: true, card: "AA-936" },
  { method: "PATCH", pattern: new RegExp(`^trips/${UUID}$`), tenant: true, card: "AA-936" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/(stage|owner|notes)$`), tenant: true, card: "AA-936" },
  { method: "POST", pattern: new RegExp(`^tasks/${UUID}/status$`), tenant: true, card: "AA-936" },
  // AA-937 (CRM-09): the reveal now carries a reason code (same path as AA-936).
  { method: "POST", pattern: new RegExp(`^travelers/${UUID}/reveal$`), tenant: true, card: "AA-936/AA-937" },
  { method: "GET", pattern: /^members$/, tenant: true, card: "AA-936" },
  { method: "GET", pattern: /^intake-forms$/, tenant: true, card: "AA-936" },
  { method: "POST", pattern: /^intake-forms$/, tenant: true, card: "AA-936" },
  { method: "PATCH", pattern: new RegExp(`^intake-forms/${UUID}$`), tenant: true, card: "AA-936" },
  // AA-937 (CRM-09): clients (A-06 / A-07), the party of a trip, documents.
  { method: "GET", pattern: /^travelers$/, tenant: true, card: "AA-937" },
  { method: "POST", pattern: /^travelers$/, tenant: true, card: "AA-937" },
  { method: "POST", pattern: /^travelers\/search$/, tenant: true, card: "AA-937" },
  { method: "GET", pattern: new RegExp(`^travelers/${UUID}$`), tenant: true, card: "AA-937" },
  { method: "PATCH", pattern: new RegExp(`^travelers/${UUID}(/profile)?$`), tenant: true, card: "AA-937" },
  { method: "PUT", pattern: new RegExp(`^travelers/${UUID}/health$`), tenant: true, card: "AA-937" },
  { method: "POST", pattern: new RegExp(`^travelers/${UUID}/relations$`), tenant: true, card: "AA-937" },
  { method: "DELETE", pattern: new RegExp(`^travelers/${UUID}/relations/${UUID}$`), tenant: true, card: "AA-937" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/travelers$`), tenant: true, card: "AA-937" },
  { method: "DELETE", pattern: new RegExp(`^trips/${UUID}/travelers/${UUID}$`), tenant: true, card: "AA-937" },
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/documents$`), tenant: true, card: "AA-937" },
  { method: "POST", pattern: /^documents$/, tenant: true, card: "AA-937", maxBody: DOCUMENT_BODY_BYTES },
  { method: "POST", pattern: new RegExp(`^documents/${UUID}/download$`), tenant: true, card: "AA-937" },
  { method: "PATCH", pattern: new RegExp(`^documents/${UUID}$`), tenant: true, card: "AA-937" },
  { method: "DELETE", pattern: new RegExp(`^documents/${UUID}$`), tenant: true, card: "AA-937" },
  // AA-940 (CRM-12): the access log (owner only, the API checks) and its download.
  { method: "GET", pattern: /^audit\/events$/, tenant: true, card: "AA-940" },
  { method: "GET", pattern: /^audit\/events\/export$/, tenant: true, card: "AA-940" },
  // AA-939 (CRM-11): Team & roles, plan, owner recovery banner; the invitation accept (no agency yet: the token decides).
  { method: "GET", pattern: /^team$/, tenant: true, card: "AA-939" },
  { method: "POST", pattern: /^team\/invitations$/, tenant: true, card: "AA-939" },
  { method: "DELETE", pattern: new RegExp(`^team/invitations/${UUID}$`), tenant: true, card: "AA-939" },
  { method: "PATCH", pattern: new RegExp(`^team/members/${UUID}$`), tenant: true, card: "AA-939" },
  { method: "POST", pattern: new RegExp(`^team/members/${UUID}/remove$`), tenant: true, card: "AA-939" },
  { method: "GET", pattern: /^team\/reassign-queue$/, tenant: true, card: "AA-939" },
  { method: "POST", pattern: /^team\/reassign$/, tenant: true, card: "AA-939" },
  { method: "GET", pattern: /^plan$/, tenant: true, card: "AA-939" },
  { method: "GET", pattern: /^recovery$/, tenant: true, card: "AA-939" },
  { method: "POST", pattern: new RegExp(`^recovery/${UUID}/cancel$`), tenant: true, card: "AA-939" },
  { method: "POST", pattern: /^invitations\/accept$/, tenant: false, card: "AA-939" },
  // AA-942 (CRM-14): data lifecycle — rules and settings, legal holds, the trash, delete / restore, archive, client
  // erasure, agency offboarding (export download, erase). Owner-only checks are the API's.
  { method: "GET", pattern: /^lifecycle$/, tenant: true, card: "AA-942" },
  { method: "PATCH", pattern: /^lifecycle\/settings$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: /^lifecycle\/holds$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: new RegExp(`^lifecycle/holds/${UUID}/release$`), tenant: true, card: "AA-942" },
  { method: "GET", pattern: /^trash$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: new RegExp(`^trash/(trip|traveler|task|note|document|booking_line|agency_supplier)/${UUID}/restore$`), tenant: true, card: "AA-942/AA-944" },
  { method: "DELETE", pattern: new RegExp(`^(trips|travelers|tasks|notes)/${UUID}$`), tenant: true, card: "AA-942" },
  { method: "POST", pattern: new RegExp(`^travelers/${UUID}/(archive|unarchive|erasure)$`), tenant: true, card: "AA-942" },
  { method: "GET", pattern: /^lifecycle\/offboarding$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: /^lifecycle\/offboarding$/, tenant: true, card: "AA-942" },
  { method: "POST", pattern: new RegExp(`^lifecycle/offboarding/${UUID}/(cancel|erase)$`), tenant: true, card: "AA-942" },
  // AA-941 (CRM-13a): the owner confirms an export / deletion request AA raised (decline = cancel).
  { method: "POST", pattern: new RegExp(`^lifecycle/offboarding/${UUID}/accept$`), tenant: true, card: "AA-941" },
  { method: "GET", pattern: new RegExp(`^lifecycle/offboarding/${UUID}/export$`), tenant: true, card: "AA-942" },
  // AA-967 (CRM-35): portability exports (list, queue, single-use link, the ZIP download) and the data-request log.
  // Owner-only checks are the API's.
  { method: "GET", pattern: /^data\/exports$/, tenant: true, card: "AA-967" },
  { method: "POST", pattern: /^data\/exports$/, tenant: true, card: "AA-967" },
  { method: "POST", pattern: new RegExp(`^data/exports/${UUID}/link$`), tenant: true, card: "AA-967" },
  { method: "GET", pattern: new RegExp(`^data/exports/${UUID}/download$`), tenant: true, card: "AA-967", timeoutMs: 50_000 },
  { method: "GET", pattern: /^data\/requests$/, tenant: true, card: "AA-967" },
  { method: "POST", pattern: /^data\/requests$/, tenant: true, card: "AA-967" },
  { method: "POST", pattern: new RegExp(`^data/requests/${UUID}/(extend|close)$`), tenant: true, card: "AA-967" },
  // AA-943 (CRM-15): Undo of a move, cancel (dry run first: ?dry_run=true|false), bulk moves and their Undo, the
  // Traveller info panel (Stage 2: read, send the planning form, fill in from a call), Settings > Forms (timezone,
  // auto-send, call slots; owner only, the API checks).
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/stage/undo$`), tenant: true, card: "AA-943" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/cancel$`), tenant: true, card: "AA-943" },
  { method: "POST", pattern: /^pipeline\/bulk-stage$/, tenant: true, card: "AA-943" },
  { method: "POST", pattern: /^pipeline\/bulk-stage\/undo$/, tenant: true, card: "AA-943" },
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/stage2$`), tenant: true, card: "AA-943" },
  { method: "PUT", pattern: new RegExp(`^trips/${UUID}/stage2$`), tenant: true, card: "AA-943" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/stage2/send$`), tenant: true, card: "AA-943" },
  { method: "GET", pattern: /^settings\/pipeline$/, tenant: true, card: "AA-943" },
  { method: "PUT", pattern: /^settings\/pipeline$/, tenant: true, card: "AA-943" },
  // AA-944 (CRM-16): suppliers (A-13, contact reveal with a reason), a trip's booking lines (add, edit, confirm,
  // cancel, delete a planning one, money), the A-11 ledger, Settings > Payments and FX overrides (owner; the API
  // checks the roles and strips margins for the assistant).
  { method: "GET", pattern: /^suppliers$/, tenant: true, card: "AA-944" },
  { method: "POST", pattern: /^suppliers$/, tenant: true, card: "AA-944" },
  { method: "PATCH", pattern: new RegExp(`^suppliers/${UUID}$`), tenant: true, card: "AA-944" },
  { method: "DELETE", pattern: new RegExp(`^suppliers/${UUID}$`), tenant: true, card: "AA-944" },
  { method: "POST", pattern: new RegExp(`^suppliers/${UUID}/reveal$`), tenant: true, card: "AA-944" },
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/lines$`), tenant: true, card: "AA-944" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/lines$`), tenant: true, card: "AA-944" },
  { method: "PATCH", pattern: new RegExp(`^lines/${UUID}$`), tenant: true, card: "AA-944" },
  { method: "DELETE", pattern: new RegExp(`^lines/${UUID}$`), tenant: true, card: "AA-944" },
  { method: "POST", pattern: new RegExp(`^lines/${UUID}/(confirm|cancel)$`), tenant: true, card: "AA-944" },
  { method: "PUT", pattern: new RegExp(`^lines/${UUID}/money$`), tenant: true, card: "AA-944" },
  { method: "GET", pattern: /^bookings$/, tenant: true, card: "AA-944" },
  // AA-964 (CRM-33a): Settings > AI (every role reads, the owner changes) and one AI draft for a trip (a suggestion,
  // never sent from here).
  { method: "GET", pattern: /^settings\/ai$/, tenant: true, card: "AA-964" },
  { method: "PUT", pattern: /^settings\/ai$/, tenant: true, card: "AA-964" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/ai/draft$`), tenant: true, card: "AA-964", timeoutMs: 45_000 },
  { method: "GET", pattern: /^settings\/payments$/, tenant: true, card: "AA-944" },
  { method: "PUT", pattern: /^settings\/payments$/, tenant: true, card: "AA-944" },
  { method: "GET", pattern: /^fx\/overrides$/, tenant: true, card: "AA-944" },
  { method: "POST", pattern: /^fx\/overrides$/, tenant: true, card: "AA-944" },
  // AA-947 (CRM-19): AA supply: the catalog cache search, one product's parts / departures / "from" price (live from
  // Booking), a new AA line from a quote, and the asks on an AA line (the line itself is never edited).
  { method: "GET", pattern: /^aa\/catalog$/, tenant: true, card: "AA-947" },
  { method: "GET", pattern: /^aa\/products\/[A-Za-z0-9._:%-]{1,200}$/, tenant: true, card: "AA-947" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/aa-lines$`), tenant: true, card: "AA-947" },
  { method: "POST", pattern: new RegExp(`^lines/${UUID}/aa/(hold|confirm|release|requote)$`), tenant: true, card: "AA-947" },
  // AA-950 (CRM-21b): the client side seen from the agency: re-issue the client's link (owner / agent; send is the
  // AA-946 line), what the client did with a version (viewed, hold, choices R19), and the bell (my own unseen events).
  { method: "POST", pattern: new RegExp(`^proposals/${UUID}/reissue-link$`), tenant: true, card: "AA-950" },
  { method: "GET", pattern: new RegExp(`^proposals/${UUID}/client-activity$`), tenant: true, card: "AA-950" },
  { method: "GET", pattern: /^bell$/, tenant: true, card: "AA-950" },
  { method: "POST", pattern: /^bell\/seen$/, tenant: true, card: "AA-950" },
  // AA-949 (CRM-21a): the day-list builder: create (Stage 2 skeleton or a working copy), the page, settings, days
  // (add, edit, delete, order), blocks, agency and AA items (live quote), rules, Refresh AA prices, Publish vN. Roles,
  // money fields and the version counter are the API's.
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/builder$`), tenant: true, card: "AA-949" },
  { method: "GET", pattern: new RegExp(`^proposals/${UUID}/builder$`), tenant: true, card: "AA-949" },
  { method: "PATCH", pattern: new RegExp(`^proposals/${UUID}/builder$`), tenant: true, card: "AA-949" },
  { method: "POST", pattern: new RegExp(`^proposals/${UUID}/(days|rules|aa-refresh|publish)$`), tenant: true, card: "AA-949" },
  { method: "PUT", pattern: new RegExp(`^proposals/${UUID}/days/order$`), tenant: true, card: "AA-949" },
  { method: "PATCH", pattern: new RegExp(`^proposals/${UUID}/(days|blocks|options)/${UUID}$`), tenant: true, card: "AA-949" },
  { method: "DELETE", pattern: new RegExp(`^proposals/${UUID}/(days|blocks|options|rules)/${UUID}$`), tenant: true, card: "AA-949" },
  { method: "POST", pattern: new RegExp(`^proposals/${UUID}/days/${UUID}/blocks$`), tenant: true, card: "AA-949" },
  { method: "POST", pattern: new RegExp(`^proposals/${UUID}/blocks/${UUID}/(options|aa-options)$`), tenant: true, card: "AA-949" },
  // AA-951 (CRM-22): the text proposal (A-05: e-mail / WhatsApp text, EN / VI, preview and send with the version's
  // price hash; a stale hash is the API's 409), the builder's Before you go day 0 (packs from CIS), and Settings >
  // Templates (the library, the AA starter import; owner writes, the API checks).
  { method: "GET", pattern: new RegExp(`^proposals/${UUID}/text$`), tenant: true, card: "AA-951" },
  { method: "POST", pattern: new RegExp(`^proposals/${UUID}/text/send$`), tenant: true, card: "AA-951" },
  { method: "GET", pattern: new RegExp(`^proposals/${UUID}/before-you-go$`), tenant: true, card: "AA-951" },
  { method: "PUT", pattern: new RegExp(`^proposals/${UUID}/before-you-go$`), tenant: true, card: "AA-951" },
  { method: "GET", pattern: /^message-templates$/, tenant: true, card: "AA-951" },
  { method: "POST", pattern: /^message-templates(\/import-aa)?$/, tenant: true, card: "AA-951" },
  { method: "PATCH", pattern: new RegExp(`^message-templates/${UUID}$`), tenant: true, card: "AA-951" },
  { method: "DELETE", pattern: new RegExp(`^message-templates/${UUID}$`), tenant: true, card: "AA-951" },
  // AA-945 (CRM-17): Settings > Mailbox (own mailbox: every role; the agency's: owner, the API checks), the forward-in
  // address, the sender table, conversations (trip Messages tab, Unmatched inbox, link to a client), drafts (autosave,
  // discard), send / Undo / back to draft, saved replies, the history import (S252: the raw mbox / CSV upload and its
  // progress).
  { method: "GET", pattern: /^mailbox$/, tenant: true, card: "AA-945" },
  { method: "DELETE", pattern: /^mailbox$/, tenant: true, card: "AA-945" },
  { method: "POST", pattern: /^mailbox\/(oauth\/start|oauth\/complete|test)$/, tenant: true, card: "AA-945" },
  { method: "PUT", pattern: /^mailbox\/smtp$/, tenant: true, card: "AA-945" },
  { method: "GET", pattern: /^mailbox\/ingest-address$/, tenant: true, card: "AA-945" },
  { method: "GET", pattern: /^mail\/senders$/, tenant: true, card: "AA-945" },
  { method: "POST", pattern: /^mail\/imports$/, tenant: true, card: "AA-945", maxBody: MAIL_IMPORT_BODY_BYTES, raw: true, timeoutMs: 50_000 },
  { method: "GET", pattern: new RegExp(`^mail/imports/${UUID}$`), tenant: true, card: "AA-945" },
  // AA-962 (CRM-31): onboarding (checklist marks, controller settings, the shared-responsibility acknowledgement), the
  // client import (raw CSV / .xlsx preview and start, progress), duplicate clients and the merge. Roles: the API's.
  { method: "GET", pattern: /^onboarding$/, tenant: true, card: "AA-962" },
  { method: "PUT", pattern: /^onboarding\/steps\/[a-z_]{3,40}$/, tenant: true, card: "AA-962" },
  { method: "PUT", pattern: /^onboarding\/controller$/, tenant: true, card: "AA-962" },
  { method: "POST", pattern: /^onboarding\/acknowledgement$/, tenant: true, card: "AA-962" },
  { method: "POST", pattern: /^client-imports(\/preview)?$/, tenant: true, card: "AA-962", maxBody: CLIENT_IMPORT_BODY_BYTES, raw: true, timeoutMs: 50_000 },
  { method: "GET", pattern: /^client-imports$/, tenant: true, card: "AA-962" },
  { method: "GET", pattern: new RegExp(`^client-imports/${UUID}$`), tenant: true, card: "AA-962" },
  { method: "GET", pattern: /^client-duplicates$/, tenant: true, card: "AA-962" },
  { method: "POST", pattern: new RegExp(`^travelers/${UUID}/merge$`), tenant: true, card: "AA-962" },
  { method: "GET", pattern: /^conversations$/, tenant: true, card: "AA-945" },
  { method: "GET", pattern: new RegExp(`^conversations/${UUID}/messages$`), tenant: true, card: "AA-945" },
  { method: "POST", pattern: new RegExp(`^conversations/${UUID}/link$`), tenant: true, card: "AA-945" },
  { method: "PUT", pattern: new RegExp(`^messages/drafts/${UUID}$`), tenant: true, card: "AA-945" },
  { method: "DELETE", pattern: new RegExp(`^messages/drafts/${UUID}$`), tenant: true, card: "AA-945" },
  { method: "POST", pattern: /^messages$/, tenant: true, card: "AA-945" },
  { method: "POST", pattern: new RegExp(`^messages/${UUID}/(undo|edit)$`), tenant: true, card: "AA-945" },
  { method: "GET", pattern: /^saved-replies$/, tenant: true, card: "AA-945" },
  { method: "POST", pattern: /^saved-replies$/, tenant: true, card: "AA-945" },
  { method: "PATCH", pattern: new RegExp(`^saved-replies/${UUID}$`), tenant: true, card: "AA-945" },
  { method: "DELETE", pattern: new RegExp(`^saved-replies/${UUID}$`), tenant: true, card: "AA-945" },
  // AA-946 (CRM-18): the trip's Proposal tab (proposals, send / withdraw, the payment schedule, mark paid / void,
  // portal invites per traveller and revoke), Settings > Agreements (owner; the API checks the roles).
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/lifecycle$`), tenant: true, card: "AA-946" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/proposals$`), tenant: true, card: "AA-946" },
  { method: "PATCH", pattern: new RegExp(`^proposals/${UUID}$`), tenant: true, card: "AA-946" },
  { method: "POST", pattern: new RegExp(`^proposals/${UUID}/(send|withdraw)$`), tenant: true, card: "AA-946" },
  { method: "POST", pattern: new RegExp(`^schedule/${UUID}/(mark-paid|void)$`), tenant: true, card: "AA-946" },
  // AA-952 (CRM-23 slice 2): invoices (the trip's list and new drafts, A-10 receivables, one invoice, edit a draft,
  // issue, void, the printable document) and Settings > Payments "Invoices" (owner; the API checks the roles).
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/invoices$`), tenant: true, card: "AA-952" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/invoices$`), tenant: true, card: "AA-952" },
  { method: "GET", pattern: /^invoices$/, tenant: true, card: "AA-952" },
  { method: "GET", pattern: new RegExp(`^invoices/${UUID}(/document)?$`), tenant: true, card: "AA-952" },
  { method: "PATCH", pattern: new RegExp(`^invoices/${UUID}$`), tenant: true, card: "AA-952" },
  { method: "POST", pattern: new RegExp(`^invoices/${UUID}/(issue|void|reject-claim)$`), tenant: true, card: "AA-952" },
  { method: "GET", pattern: /^settings\/invoices$/, tenant: true, card: "AA-952" },
  { method: "PUT", pattern: /^settings\/invoices$/, tenant: true, card: "AA-952" },
  // AA-952 (CRM-23 slice 3): payments on an invoice, refund requests (ask, list, approve / reject, mark refunded) and
  // Settings > Payments "How clients pay you" (owner; the API checks the roles and the refund threshold).
  { method: "POST", pattern: new RegExp(`^invoices/${UUID}/payments$`), tenant: true, card: "AA-952" },
  { method: "POST", pattern: new RegExp(`^payments/${UUID}/refund-requests$`), tenant: true, card: "AA-952" },
  { method: "GET", pattern: /^refund-requests$/, tenant: true, card: "AA-952" },
  { method: "POST", pattern: new RegExp(`^refund-requests/${UUID}/(approve|reject|execute)$`), tenant: true, card: "AA-952" },
  { method: "GET", pattern: /^settings\/payment-methods$/, tenant: true, card: "AA-952" },
  { method: "PUT", pattern: /^settings\/payment-methods\/manual$/, tenant: true, card: "AA-952" },
  { method: "POST", pattern: new RegExp(`^settings/payment-methods/${UUID}/disconnect$`), tenant: true, card: "AA-952" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/portal-access$`), tenant: true, card: "AA-946" },
  { method: "POST", pattern: new RegExp(`^portal-access/${UUID}/revoke$`), tenant: true, card: "AA-946" },
  { method: "GET", pattern: /^agreements$/, tenant: true, card: "AA-946" },
  { method: "POST", pattern: /^agreements$/, tenant: true, card: "AA-946" },
  { method: "PATCH", pattern: new RegExp(`^agreements/${UUID}$`), tenant: true, card: "AA-946" },
  { method: "POST", pattern: new RegExp(`^agreements/${UUID}/publish$`), tenant: true, card: "AA-946" },
  // AA-955 (CRM-25): the trip's signed documents and the sealed envelope of one signature (a reveal with a reason).
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/agreements$`), tenant: true, card: "AA-955" },
  { method: "POST", pattern: new RegExp(`^signatures/${UUID}/envelope$`), tenant: true, card: "AA-955" },
  // AA-961 (CRM-30): Travelling now (no names), the reach list, the logged bulk reveal (a reason above 5, an hourly
  // limit: the API's), the trip's AA incident flags; the calendar, my ICS feed (make / revoke; the owner's team list).
  { method: "GET", pattern: /^duty\/(travelling|reach)$/, tenant: true, card: "AA-961" },
  { method: "POST", pattern: /^duty\/reveal$/, tenant: true, card: "AA-961" },
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/incidents$`), tenant: true, card: "AA-961" },
  { method: "GET", pattern: /^calendar(\/feed|\/feeds)?$/, tenant: true, card: "AA-961" },
  { method: "POST", pattern: /^calendar\/feed$/, tenant: true, card: "AA-961" },
  { method: "POST", pattern: new RegExp(`^calendar/feeds/${UUID}/revoke$`), tenant: true, card: "AA-961" },
  // AA-966 (CRM-34): the Reports page, the Home tiles and one section as CSV (money sections: the API's view_margin).
  { method: "GET", pattern: /^reports\/(summary|tiles|export)$/, tenant: true, card: "AA-966" },
  // AA-971 (CRM-13b): AA support access (every member reads it for the banner; the owner grants and ends it).
  { method: "GET", pattern: /^support-access$/, tenant: true, card: "AA-971" },
  { method: "POST", pattern: /^support-access$/, tenant: true, card: "AA-971" },
  { method: "POST", pattern: /^support-access\/revoke$/, tenant: true, card: "AA-971" },
  // AA-956 (CRM-26): the trip's AA ground operations panel (counts and dates only) and the agency's withdrawal.
  { method: "GET", pattern: new RegExp(`^trips/${UUID}/ops-release$`), tenant: true, card: "AA-956" },
  { method: "POST", pattern: new RegExp(`^trips/${UUID}/ops-release/withdraw$`), tenant: true, card: "AA-956" },
];

/** Calls only the web server makes (lib/crmApi.ts crmCall), never reachable through /bff from the browser. */
export const SERVER_ONLY_ROUTES: readonly BffRoute[] = [
  // AA-940: the sign-in event, sent by /auth/agency after a real sign-in or a chooser pick (a browser route would let
  // any member add sign-in events at will).
  { method: "POST", pattern: /^audit\/sign-in$/, tenant: true, card: "AA-940" },
];

export function matchBff(method: string, subPath: string, routes: readonly BffRoute[] = BFF_ROUTES): BffRoute | null {
  const m = method.toUpperCase();
  return routes.find((r) => r.method === m && r.pattern.test(subPath)) ?? null;
}

/**
 * Request headers the BFF builds for the upstream call. Nothing from the browser is copied except the body type:
 * JSON, or for a raw route (AA-945 upload) always application/octet-stream.
 */
export function upstreamHeaders(
  accessToken: string,
  tenantId: string | null,
  contentType: string | null,
  requestId: string,
  raw = false,
): Headers {
  const h = new Headers({ Accept: "application/json", Authorization: `Bearer ${accessToken}`, "X-Request-Id": requestId });
  if (tenantId) h.set("X-AA-Tenant-Id", tenantId);
  if (raw) h.set("Content-Type", "application/octet-stream");
  else if (contentType && contentType.toLowerCase().startsWith("application/json")) h.set("Content-Type", "application/json");
  return h;
}
