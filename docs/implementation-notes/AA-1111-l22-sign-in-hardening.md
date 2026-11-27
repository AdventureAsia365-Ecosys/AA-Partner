# AA-1111 / AA-1166 — CRM cookie prefix slice (S294, lane l22, DRAFT until the sign-in change window)

## Decisions

- **`__Secure-` prefix on the five client-host cookies** (`aa_proposal_link`, `aa_portal_link`, `aa_portal_s`,
  `aa_partner_link`, `aa_partner_s`). They are path-scoped to `/crm/public/v1/...`, so `__Host-` (needs `Path=/`)
  does not fit. The browser accepts a `__Secure-` cookie only when it was set with `Secure` from an HTTPS origin.
- **Clean rename, no fallback read of the old names**: the old cookies simply stop being read and expire on their
  own (12 h sessions, 30-day link cookies); a client opens their link again (the link tokens stay valid). A fallback
  would keep accepting a cookie without the prefix, which is what the rename removes.
- **The landings clear the pre-rename link cookie (review fix).** Each `/c/p`, `/c/t`, `/c/a` landing appends a second
  `Set-Cookie` that clears its old cookie (`aa_proposal_link` / `aa_portal_link` / `aa_partner_link`, `Max-Age=0`,
  same path, `Secure`), so a still-valid link token does not sit unread in the browser for up to 30 days. A
  `TODO(AA-1111)` removes the clears 30 days after this ships. The old session cookies (`aa_portal_s`, `aa_partner_s`)
  are not cleared: they last 12 h, hold no link token, and the API does not read them.
- **`directory.py` follows the Infra partner pool password policy in the same window (review fix).** Its comments
  describe AA-CIS-Infra #1041: at least 14 characters, no character-class rules, a 1-day temporary password
  (`temporary_password_validity_days`). The 24-character temporary password keeps every class (harmless, and still
  valid if classes come back).
- **Self-service password change is not in this PR**: root ADR 0006 decision 2 keeps the Cognito self-service scope
  off every app client, so a change-password page needs a tech-lead decision first (see the lane's follow-ups).

## Changed

- API: the five cookie constants (`portal.SESSION_COOKIE` in `app/portal/public.py`; `partner.SESSION_COOKIE` and
  `partner.LINK_COOKIE` in `app/partners/public.py`; `client_pages.PROPOSAL_COOKIE` and
  `client_pages.PORTAL_LINK_COOKIE` in `app/routers/client_pages.py`) and their docstrings; the `partner_pages.py`
  docstring; the cookie literal in `tests/unit/test_esign_routes.py`; `tests/unit/test_aa1111_cookie_prefixes.py`
  (prefix on every name, the web sets the same names, every `set_cookie` / `delete_cookie` names a prefixed constant
  and passes `secure=True`, because a browser drops a `__Secure-` clear without `Secure`).
- API: the `app/members/directory.py` comments describe the Infra #1041 partner pool password policy (see Decisions).
- Web: the three link-cookie constants (`lib/clientProposal.ts`, `lib/clientPortal.ts`, `lib/partners.ts`) and the
  `/c/*` landing comments; e2e `client-proposal.spec.ts` reads the new name.
- Web: the `/c/p`, `/c/t`, `/c/a` landings clear the pre-rename link cookie; `tests/clientProposal.test.ts` and
  `tests/clientPortal.test.ts` check both `Set-Cookie` headers, `tests/partners.test.ts` has a new `/c/a` landing test.
- Docs: `docs/implementation-notes/AA-953-write-limit.md` uses the new cookie names (a note at the top; the AA-950 /
  AA-953 history is unchanged).

## Tradeoffs

- The API and the web deploy separately: for the minutes between the two deploys a link cookie set by one side is not
  read by the other, and the client sees "link not found" until they open the link again. Dev only, no real clients.

## Should know

- Merge in the same window as the AA-CIS-Infra partner pool password policy apply (#1041), so the `directory.py`
  comments never describe a policy that is not deployed.
- CloudFront forwards every viewer cookie to the CRM origins (managed AllViewer policy), so no edge change is needed.
- The kit language cookie `aa_crm_locale` keeps its name: a non-sensitive preference that page script reads.
