---
id: SEC-01-crm-controls
title: "AA-Partner input validation and sign-in protections"
description: Where each kind of input the CRM accepts is checked and how, and what protects the agency sign-in against password guessing and automated sign-in attempts.
type: reference
status: draft
owner: nghiep
audience: [engineer, agent, cio]
applies_to: [aa-crm-api, aa-crm-web]
last_reviewed: 2026-11-26
review_every_days: 90
related: [SEC-01, OPS-01-crm-slo, ARCH-06-partner]
---

# AA-Partner input validation and sign-in protections

One page that lists, per kind of data, where the CRM checks its input, and what protects the agency sign-in. It
summarises rules that live in code and in `CONTEXT.md`; when they differ, the code wins and this page is fixed in the
same PR. The ecosystem security baseline is the root `docs/security/baseline.md` (SEC-01).

## Input validation per data item

Rule of the house: every request body is a Pydantic v2 model (most with `extra="forbid"`), every path id is typed,
and a value that fails its check is refused with 422 and a stable error code before any database work. The database
then repeats the important rules as `CHECK` constraints, so a bug in one layer is caught by the other.

| Data item | Where it enters | Check | Enforced in |
|---|---|---|---|
| Request body size | every route | 1 MiB by default, the upload routes their own cap; a slow large body is cut off | `api/app/bodylimit.py` (413 `body_too_large`, 408 `body_too_slow`) |
| Record ids | path and body of the agency routes | UUID type; the row must belong to the caller's agency (row level security) | FastAPI path types; `tenant_tx()` |
| Signed client and partner links | `/c/*` landings, public routes | shape check, signature, purpose, expiry; any unusable link answers 404 `link_not_found` | `app/routers/client_pages.py`, `app/partners/public.py` |
| E-mail address | travellers, members, intake forms | trimmed and lower-cased before it is indexed; stored encrypted | `app/vault/blind_index.py` |
| Phone number | travellers, text messaging | parsed and normalised to E.164 (libphonenumber); a number that does not parse is stored but never matched | `app/vault/blind_index.py`, `app/messaging/webhook.py` |
| Free text (names, notes, messages) | agency routes, client pages, intake | `max_length` on each model field; no HTML is rendered from it (React escapes on output, invoices escape every value) | the route models; `api/app/invoices/render.py` |
| Money and currency | proposals, ledger, payments | decimal amounts, ISO 4217 currency codes, database `CHECK` constraints on kinds and signs | the route models; the `crm-0NN` migrations |
| Dates and times | trips, tasks, schedules | ISO 8601 types; agency-local day rules in the service layer | the route models |
| Uploaded documents | document upload, passport scan | allowed types only (PDF, JPEG, PNG, WebP, HEIC), the type is read from the file's own bytes (magic numbers) and must match the declared one, size cap per route | `app/documents/service.py` |
| Inbound mail HTML | mail import | kept as received and shown only inside a sandboxed frame with no script, no same-origin access and its own content security policy | `web/lib/mail.ts` (`htmlFrame`) |
| Webhook calls (text messaging, payments) | vendor webhooks | the vendor's request signature is verified before the body is read for meaning | `app/messaging/signature.py`, `app/payments/` |
| Query and paging parameters | list routes | typed, bounded page sizes | the route signatures |

## Sign-in protections

Agency members sign in through the Cognito managed login of the partner pool; the CRM never sees a password.

- **Second factor required** for every member: password, then an authenticator app code (pool MFA `ON`, no SMS).
- **No self sign-up and no self-service reset**: people are created only by the invitation flow, and a lost factor is
  reset by an administrator after the agency owner confirms.
- **Repeated failures**: Cognito applies its own throttling and temporary lockout after repeated failed sign-ins for
  one user. The behaviour is defined by AWS for the pool's tier and is not configurable in Terraform.
- **No account discovery**: the app clients answer the same way for an unknown user and a wrong password
  (`prevent_user_existence_errors`).
- **The CRM's own public routes** (invitation claim, client e-mail codes, intake forms) have per-address limits. Their
  values are settings (`crm.rate.*`) or the `RATE_LIMIT_*` values listed in `CONTEXT.md`; a refused call answers 429
  `rate_limited` with `Retry-After`.
- **Failed sign-in callbacks** are logged by the CRM web (`crm_web_sign_in_failed`, code and status only).

## Tokens in URLs

Some links carry a random capability token in their path because the client opening them has no session yet: the
client link landings (`/c/p`, `/c/t`, `/c/a`), the calendar feed and the unsubscribe link. Each token is random, made
for one purpose, stored only as a hash and can be revoked. The landings exchange the token for a host-only cookie at
once and redirect to a URL without it, and those pages send `Referrer-Policy: no-referrer`.
