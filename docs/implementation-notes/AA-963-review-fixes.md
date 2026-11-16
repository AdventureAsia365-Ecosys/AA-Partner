# AA-963 review fixes — CRM global search before it is switched on

S282 lane l12 (Claude Code). Source: the independent pre-enable review of the CRM global search (AA-Partner PR #324,
merge 74290643): 11 confirmed findings, each lowered to "low" by two skeptic votes. This file records how each one is
fixed; `AA-963.md` stays the design record of the search itself.

## Decisions

- **Folded length (finding: a term that folds to nothing matched every non-PII record).** `classify()` checks the
  minimum twice: on the collapsed raw term (shorter -> mode `too_short`, nothing read, as before) and on the
  accent-folded term (`fold(text).strip()`): long enough raw but shorter once folded -> 422 `query_too_short` (lone
  combining marks fold to nothing, an NFD `à` to `a`). `fold.contains_sql` also refuses an empty folded needle in SQL
  (`btrim(fold($1)) <> ''`), so the database fails safe on its own.
- **No echo of the term (findings: the default 422 echoed the term or another key's value; a 201-character term gave a
  generic error).** `POST /search` parses its body itself (`routers/search._search_body`, the pattern of
  `travellers._document_body`): an over-long term answers the app's own 422 `query_too_long`, anything else 422
  `invalid_query`; both carry a code and a message, never the term, an extra key's name or its value. The body bound
  rose from 200 to 1000 characters (a technical request bound): the policy is the setting
  `crm.search.query_max_chars`, checked on the collapsed term, so a short term with long space runs is no longer
  refused by the bound. Unit tests assert that neither the 422 body nor any captured log line contains the term.
- **Fail-closed switch (finding: a failed settings read kept the last cached value).** New
  `platform_settings.tenant_get_fail_closed()`: the agency's rows are re-read on every call (`force`, one small
  indexed read in a savepoint), and the answer is `closed` (search off) when that read fails, or when the value would
  come from a platform snapshot whose last refresh failed (`READER.last_error`). `search.is_enabled` (config and every
  search) uses it. Side effect: an approval in another API task counts at once instead of after the 30 s cache window.
  `tenant_get()` keeps its fail-open behaviour for limits, where the last good value is the right answer.
- **Audit (finding: bookings, suppliers and services skipped the list_view their list routes write).** One
  `list_view` per kind shown, reason `global_search`, naming the ids shown, for every kind whose own list page audits:
  clients (`traveler`, field `display_name`), trips (`trip`, titles are `pii:travel`), booking lines (`booking_line`),
  suppliers (`agency_supplier`), services (`agency_service`) and inclusion lines (`inclusion_item`). Message
  templates, automations, tags and WhatsApp templates have no list audit on their own pages and get none here.
- **Wildcards and `@` (finding: legitimate non-PII terms were refused).** `*` / `%` and an `@` inside a term with
  spaces only keep the **client** kind out (`Term.clients = False`); the plaintext kinds compare with `strpos`, whole
  words or exact references, never `LIKE`, so `100% Vietnam` and `Café @ Hội An` are plain text there. The answer
  carries `notice` (`partial_search_refused` / `partial_contact_search`) and the palette says clients were left out.
  An `@` term without spaces is still an e-mail (whole address or 422), and one with a wildcard is still 422
  `partial_search_refused`.
- **Booking references with space runs**: `BOOKING_SQL` collapses white space runs of `confirmation_no`
  (`regexp_replace(btrim(..), '[[:space:]]+', ' ', 'g')`), as `classify()` collapses the term; still exact.
- **No plaintext PII index proof fails closed (finding: untagged columns were skipped).** Every indexed text-like
  column of schema `crm` (text, varchar, char, citext, json, jsonb and their arrays) must carry a DM-6.1 tag
  (`pii:` / `conf:`, read case-insensitively after stripping) or be listed in the test as holding no personal data,
  with the reason (`NON_PERSONAL_NAMES` for code columns, `NON_PERSONAL_COLUMNS` for single columns). No migration:
  no existing column needed a comment, every one found is non-personal (closed-list codes, ids, random keys, the
  agency's own product names). `crm.agency_supplier.name` is listed with its reason: the supplier's business or trading
  name; the contact person's name and e-mail are encrypted. A guard keeps the list from rotting (every listed column
  must still be an indexed, untagged column).
- **Blind index wording (finding: "lossy on purpose" is false at agency scale).** Corrected in
  `api/app/vault/blind_index.py`, `api/app/search/service.py` and `api/app/travellers/service.py`; the frequency and
  equality leak to readers without the key (`crm_app`, `crm_owner`, the DB Migrate master, an RDS snapshot holder) is
  recorded as an accepted risk in the `blind_index` module docstring. `TOKEN_BYTES` is unchanged (shortening it would
  change every stored index and needs a re-index job).
- **Palette (finding: Enter during the debounce opened a result of the previous term).** Every status names the term
  it belongs to (`lib/search.ts` `SearchStatus`, `statusFor`): while the debounce has not sent the term typed now, the
  previous answer is hidden ("Searching…") and Enter opens nothing from it.
- **Content pieces (finding: a failure or a deleted piece was cached for the page lifetime).** One read per palette
  open (a `session` object per open), reused while that palette stays open; a failed read is never kept. No time-based
  TTL, so no new threshold.
- **One platform value switches search on for every agency** (the review's finding on the platform-level switch):
  already addressed by AA-CIS-App PR 1537 (the CIS settings page shows the agency scope and a warning before a
  platform-scope approval). Nothing changed here.

## Changed

- API: `app/search/query.py`, `app/search/fold.py`, `app/search/service.py`, `app/routers/search.py`,
  `app/platform_settings/__init__.py` (`tenant_get_fail_closed`), `app/vault/blind_index.py` and
  `app/travellers/service.py` (docstrings only).
- The search answer gains `notice` (null, or a refusal code naming why clients were not searched).
- Web: `lib/search.ts` (`SearchStatus`, `statusFor`, `noticeOf`, `query_too_short` refusal),
  `lib/searchContent.ts`, `components/search/SearchPalette.tsx`, `lib/i18nSearch.ts` (EN / VI strings).
- Tests: `tests/unit/test_aa963_search.py`, `tests/integration/test_aa963_search_db.py`, `web/tests/search.test.ts`,
  `web/tests/searchPalette.test.ts` (new, jsdom).

## Tradeoffs

- **A fresh settings read per search** instead of the 30 s cache: one indexed read of the agency's approved rows per
  search and per config call, in exchange for a switch that is off the moment it is turned off and off when the read
  fails.
- **Clients skipped, not refused, for a wildcard**: the member still finds suppliers, services and templates whose
  names contain `%`; the notice explains why no client shows. Refusing the whole search would hide records that hold
  no personal data.
- **Allow-list in the test, not column comments**: a COMMENT migration would be a contract file and a MIG-4 chain for
  documentation only; the allow-list sits next to the check, names each column's reason, and fails when it goes stale.

## Should know

- **Parallel lanes**: the stricter proof fails a new migration that indexes an untagged text column in `crm`. A lane
  that adds one must tag the column (`COMMENT ... 'pii:<class>; ...'` or `conf:`) or add it to the test's allow-list
  with a reason.
- Follow-ups (not in this PR): (1) the root design doc (`docs/audits/2026-10-21-S246-crm-design-v3/design-v3.md` §4.2
  threat model) should record the token frequency / equality risk as accepted, with the readers who can see it;
  (2) DM-6.1 `online_id` tagging of `crm.member.cognito_sub` and `crm.support_session.staff_sub` (indexed plaintext
  staff identifiers: tagging them needs a decision on whether staff sign-in ids count as personal data here, and the
  check would then need a staff-identifier exception); (3) a settings-backed TTL is not needed for the Content pieces
  (read per open).
- `TOKEN_BYTES` was deliberately not changed (lane instruction).
