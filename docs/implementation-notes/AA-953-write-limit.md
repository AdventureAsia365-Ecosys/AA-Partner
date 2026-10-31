# AA-953 follow-up — a separate write limit for the signed-in traveller portal (lane l9)

Dev E2E run 41174840671: `lifecycle-thin.spec.ts` (CRM-18) got 429 `rate_limited` (0.8 ms, at the limiter, before any
read) on `PUT /crm/public/v1/traveller/stage3`. Every client-page write called `_write_limit()`, one per-address
bucket (`app.state.intake_limiter`, `RATE_LIMIT_INTAKE_PER_MINUTE` = 5) built against inquiry-form spam. Since #145
(per-person portal) a signed-in traveller makes many writes (Stage 3, flights, passport scan, signing code, sign,
companion invite, switch, sign-out); the runner had made 7 public writes from its IP in the window. A family on one
home address would hit the same wall.

## Decisions

- **Two buckets, same key (client address, IPv6 per /64), same 429.** New setting
  `rate_limit_portal_write_per_minute` (env `RATE_LIMIT_PORTAL_WRITE_PER_MINUTE`, default **30** in code, 0 = off,
  < 0 refused at start like the siblings) and `app.state.portal_write_limiter` in `main.py`. The 429 body
  (`{"detail": "rate_limited"}`) and `Retry-After: 60` are unchanged (`routers.public._limit`).
- **Which bucket is decided from the request, never from the database** (the limit still runs before any read):
  - `_write_limit(request)`: the intake bucket, for anonymous / pre-sign-in writes.
  - `_session_write_limit(request, session)`: for writes only a signed-in traveller can make. With a session cookie of
    the issued shape (`secrets.token_urlsafe(32)` = 43 URL-safe characters, the router's `_TOKEN`) → the portal bucket;
    no cookie or a malformed one → the intake bucket (such a request cannot pass the handler's session check anyway).
    The session itself is still checked by the handler, after the limit, as before.
  - `_code_limit(request, session, content_length)` for `POST .../code`: the body (32 KiB cap, no DB read) is parsed
    first; `purpose: "sign"` (the signing code, `request_code` requires `_signed_in` for it) → `_session_write_limit`;
    `sign_in` (or anything else) → the intake bucket, even with a session cookie, because it e-mails a code to someone
    who is not signed in.
- **The cookie's shape alone is not trust (review fix, second commit).** The first pass sent any request with a
  43-character `aa_portal_s` to the 30/min bucket, so a caller who is not signed in could forge one and get 30
  session-gated writes per address per minute instead of 5 (and 30 x 10 MiB passport-scan bodies). Now
  `_session_write(request, session)` is an async context manager around the handler body:
  1. no cookie of the issued shape -> the intake bucket, as before;
  2. a shaped cookie -> one slot of a third limiter, `app.state.portal_unproven_limiter`
     (`RATE_LIMIT_INTAKE_PER_MINUTE`, the anonymous ceiling, no new setting), is **reserved on entry**, then the
     portal bucket is charged (if the portal bucket says 429, the reservation is given back);
  3. the handler runs inside `app.portal.public.session_proof()`; `_signed_in` (both success returns) and
     `sign_out` (only when its `UPDATE` ended a live session) call `mark_session_proven()`; on exit, a proven request
     gives its unproven slot back (`FixedWindowLimiter.release(key, window)`), a refused one keeps it.
  So a forged or stale cookie is held to 5 refused writes per address per minute, the 6th is 429 before the body
  (10 MiB for the passport scan) or the database is read, and because the slot is taken on entry (not charged after
  the refusal) a parallel burst cannot pass before the first refusal is counted. A request that proved its session
  and then failed for another reason (403 `stage3_locked`, 422) still gives its slot back: it was signed in.
- **The proof travels in a `ContextVar` holding a mutable `SessionProof`**, not a return value: the services keep
  their signatures, and a mark made in a copied context (a task a service may start) still reaches the router.
  `FixedWindowLimiter.window` (a counter bumped at each new window) makes a late `release` a no-op, so a request that
  straddles a window never frees a slot of the next one.
- **Key is the address only, not address + session.** Keying on the session would let a forged cookie value mint a
  fresh bucket per request. 30 per address per minute covers a family of four each saving details, flights, a scan
  and a signature in the same minute; the e2e lifecycle uses 4 anonymous writes and ~4 session writes.

### Route classification (`api/app/routers/client_pages.py`)

| Route | What the handler requires | Bucket |
|---|---|---|
| `POST /p/{token}/accept` | proposal link only | intake |
| `POST /p/{token}/terms` | proposal link only | intake |
| `POST /proposal/pick` | `aa_proposal_link` cookie | intake |
| `POST /proposal/hold` | `aa_proposal_link` cookie (Booking re-check; its own `recheck_limit` too) | intake |
| `POST /proposal/accept` | `aa_proposal_link` cookie | intake |
| `POST /proposal/terms` | `aa_proposal_link` cookie | intake |
| `POST /proposal/change-request` | `aa_proposal_link` cookie | intake |
| `POST /proposal/invoices/{id}/claim` | `aa_proposal_link` cookie | intake |
| `POST /proposal/invoices/{id}/card` | `aa_proposal_link` cookie | intake |
| `POST /portal/{token}/code`, `POST /traveller/code` with `purpose: sign_in` | portal link only (e-mails a code) | intake |
| `POST /portal/{token}/code`, `POST /traveller/code` with `purpose: sign` | portal session (`_signed_in`) | portal (with a session cookie) |
| `POST /portal/{token}/session`, `POST /traveller/session` | portal link + the e-mailed code (pre-auth) | intake |
| `POST /portal/{token}/sign-out`, `POST /traveller/sign-out` | the session cookie (no-op without one) | portal (with a session cookie) |
| `PUT /portal/{token}/stage3`, `PUT /traveller/stage3` | portal session | portal (with a session cookie) |
| `POST /portal/{token}/sign`, `POST /traveller/sign` | portal session + signing code | portal (with a session cookie) |
| `POST /traveller/switch` | portal session | portal (with a session cookie) |
| `PUT /traveller/flights` | portal session | portal (with a session cookie) |
| `POST /traveller/passport-scan` | portal session | portal (with a session cookie) |
| `POST /traveller/companions` | portal session (lead) | portal (with a session cookie) |

"portal (with a session cookie)" = the intake bucket when the request carries no session cookie of the issued shape;
with one, the portal bucket plus a slot of the unproven-session bucket held until the session check passes.
The inquiry form (`POST /forms/{key}/submissions`) and Stage 2 (`POST /s/{token}`) in `routers/public.py` are
unchanged (intake bucket).

## Changed

- `api/app/settings.py`: `DEFAULT_PORTAL_WRITE_PER_MINUTE = 30`, the field, the env parse, the `>= 0` check, the
  docstring entry under "Rate limits" (and a note that the intake limit also guards anonymous client-page writes).
- `api/app/main.py`: `app.state.portal_write_limiter`.
- `api/app/routers/client_pages.py`: `_session_write_limit`, `_code_limit`; the ten session-gated handlers and the two
  code handlers call them; the module docstring describes the two buckets.
- `CONTEXT.md`: the `/crm/public/v1/p|portal` and `/traveller` route rows and the environment list.
- Review fix (second commit): `api/app/ratelimit.py` (`FixedWindowLimiter.window`, `release`), `api/app/main.py`
  (`app.state.portal_unproven_limiter`), `api/app/portal/public.py` (`SessionProof`, `session_proof()`,
  `mark_session_proven()`; marks in `_signed_in` and `sign_out`), `api/app/routers/client_pages.py`
  (`_session_write` async context manager around the ten session-gated handlers, `_code_write` for the code routes;
  replaces `_session_write_limit` / `_code_limit`). Tests: forged 43-character cookie on every session route -> five
  403 then 429; refused writes counted across routes; a valid session gives its slot back (31 writes, unproven count
  0); a parallel burst of 5 in-flight writes stops the 6th; a proven-then-refused write gives its slot back
  (`test_client_page_limits.py`); `_signed_in` / `link_not_found` / `sign_out` "UPDATE 0" vs "UPDATE 1" proof marks
  (`test_lifecycle_services.py`); `release` window semantics (`test_health.py`).
- Tests: `api/tests/unit/test_client_page_limits.py` (every route of the table: a session write passes 6 quick writes,
  an anonymous one 429s at the 6th even with a session cookie; the portal bucket 429s at the 31st; buckets separate;
  no / malformed session cookie keeps the intake bucket; 0 = off; the defaults), `test_settings.py` (default, `-1`
  and non-integer refused). The older route tests that raise `rate_limit_intake_per_minute` to 50/100 still pass
  unchanged: their stub session value (`session-token`) is not of the issued shape, so they stay on the intake bucket
  they raise.

## Tradeoffs

- **Per address, a caller who is not signed in now gets 5 anonymous writes (intake) + 5 refused session writes
  (unproven) a minute**, against 5 in all before the portal bucket existed. Folding the unproven slots into the intake
  bucket would keep exactly 5, but then a family that used its 5 anonymous writes (sign-in codes and code checks)
  would also be stopped on every signed-in write for the rest of the minute, which is the bug this issue fixes. The
  review asked for "a 5/min ceiling on writes whose session check fails"; this is that. Worst-case unauthenticated
  upload on the passport scan: 10 x 10 MiB a minute per address (5 without a cookie on intake, 5 forged), not the
  30 x 10 MiB of the first pass.
- Someone on the same address (same NAT) can use up the unproven bucket with 5 forged writes and so stop that
  household's signed-in writes for up to a minute. The intake bucket already has the same shared-address property;
  the key stays the address (a session key would let a forged value mint fresh buckets).
- A signed-in traveller whose session check fails (session expired, address since changed) uses an unproven slot per
  try; the portal answers 403 `portal_sign_in_required` and the web sends them to sign in again, so this is a few
  writes, not a loop.
- Sign-out with a cookie that ended nothing (already signed out, forged) counts as unproven: it is a no-op 200 for the
  caller either way.
- Reading the code body before choosing its bucket: a 32 KiB-capped parse, no database, no mail; kept so a sign-in
  code never gets the higher bucket.
- No per-session key (see Decisions). If families on carrier-grade NAT ever hit 30, raise the env value, not the code.

## Should know

- No Terraform change is needed: the code default (30) applies on Dev. AA-CIS-Infra `crm_app.tf` may later pin
  `RATE_LIMIT_PORTAL_WRITE_PER_MINUTE` explicitly next to `RATE_LIMIT_INTAKE_PER_MINUTE` (orchestrator decides).
- The limiter is in process memory per task (one API task on Dev), like the other buckets.
- e2e: no spec change. After deploy, rerun `e2e-dev.yml`; `lifecycle-thin` (CRM-18) should pass step 6.
- Run locally (after the review fix): every unit module touching the client pages, the portal service, the limiter
  and the settings, 387 passed (earlier pass: 166); `ruff check` and `ruff format --check` clean.
