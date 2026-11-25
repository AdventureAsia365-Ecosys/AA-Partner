# AA-1111 / AA-1166 — CRM cookie prefix slice (S294, lane l22, DRAFT until the sign-in change window)

## Decisions

- **`__Secure-` prefix on the five client-host cookies** (`aa_proposal_link`, `aa_portal_link`, `aa_portal_s`,
  `aa_partner_link`, `aa_partner_s`). They are path-scoped to `/crm/public/v1/...`, so `__Host-` (needs `Path=/`)
  does not fit. The browser accepts a `__Secure-` cookie only when it was set with `Secure` from an HTTPS origin.
- **Clean rename, no fallback read of the old names**: the old cookies simply stop being read and expire on their
  own (12 h sessions, 30-day link cookies); a client opens their link again (the link tokens stay valid). A fallback
  would keep accepting a cookie without the prefix, which is what the rename removes.
- **Self-service password change is not in this PR**: root ADR 0006 decision 2 keeps the Cognito self-service scope
  off every app client, so a change-password page needs a tech-lead decision first (see the lane's follow-ups).

## Changed

- API: the four cookie constants (`app/portal/public.py`, `app/partners/public.py`, `app/routers/client_pages.py`)
  and their docstrings; `tests/unit/test_aa1111_cookie_prefixes.py` (prefix on every name, the web sets the same
  names, every `set_cookie` / `delete_cookie` names a prefixed constant).
- Web: the three link-cookie constants (`lib/clientProposal.ts`, `lib/clientPortal.ts`, `lib/partners.ts`) and the
  `/c/*` landing comments; e2e `client-proposal.spec.ts` reads the new name.

## Tradeoffs

- The API and the web deploy separately: for the minutes between the two deploys a link cookie set by one side is not
  read by the other, and the client sees "link not found" until they open the link again. Dev only, no real clients.

## Should know

- CloudFront forwards every viewer cookie to the CRM origins (managed AllViewer policy), so no edge change is needed.
- The kit language cookie `aa_crm_locale` keeps its name: a non-sensitive preference that page script reads.
