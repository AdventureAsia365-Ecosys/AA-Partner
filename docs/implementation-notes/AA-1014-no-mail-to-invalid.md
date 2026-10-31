# AA-1014 follow-up: no mail to `.invalid` addresses

Lane l12 (S261). The CRM review runs on Dev give their invented clients `@example.invalid` addresses. Proposal send,
portal invite / reissue, the deposit-settled portal invite, the portal sign-in code and the companion invite queue
client mail that the CRM sends through the agency's connected mailbox (a real Gmail account on Dev), so every run
sent 6 to 8 mails that bounced back into that mailbox.

## Decisions

- **One rule, in the send path, every environment.** RFC 2606 / RFC 6761 reserve `invalid`: it never resolves, so the
  rule is not Dev-only. `app/mail/recipients.py` holds it (`is_invalid_tld`, `deliverable`, code `INVALID_TLD`).
- **Where it runs.** Every client mail leaves through one place, `MailSender.send` (`app/mail/sender.py`, behind the
  dispatcher gate). The check runs right after the client address is decrypted, before the from address, the subject,
  the body, the code or the credential are opened, so nothing else is decrypted for a mail that will not go out.
- **Terminal state, no migration.** The message gets `status = 'failed'`, `failure_code = 'invalid_tld'` (free code,
  `message_failure_chk` only asks for `^[a-z][a-z0-9_]{1,40}$` on `failed` / `held_reconnect`); the sender answers
  `send_failed`, so the dispatch row ends `dropped` / `send_failed` (an existing `dispatch_queue_gate_chk` value) and
  the send-marker attempt closes `send_failed`. No new status, no crm-NNN migration.
- **Also guarded (same rule, small):** the Settings > Mailbox Test button (`app/mail/health.py`: the check still runs,
  `test_mail_sent` is false) and AA's system mail (`SesMailer.send` in `app/members/mail.py`: logs
  `crm_system_mail_skipped kind=... reason=invalid_tld` and returns an empty id; no caller reads the id). A synthetic
  member at a `.invalid` address would otherwise bounce at AA's SES and count against its reputation.
- **Normalisation.** Strip, `parseaddr` (a display-name form), the part after the last `@`, IDNA dot separators
  (U+3002, U+FF0E, U+FF61) to `.`, a trailing root dot removed, Python's IDNA codec (nameprep folds case and full-width
  letters), lower-case; a domain the codec refuses (empty or over-long label) is judged on its lower-case text.
- **Only `.invalid`.** `.test` / `.example` stay sendable: the test suites and fixtures use them with fake providers,
  and the brief named `.invalid` only.

## Changed

- `app/mail/recipients.py` (new), `app/mail/sender.py`, `app/mail/health.py`, `app/members/mail.py`.
- Tests: `tests/unit/test_mail_recipients.py` (rule: all-invalid, mixed, none-invalid, case / whitespace / root dot /
  display name / IDN / full-width; sender, code mail, Test button, SES; an AST guard that every `app/mail` module
  calling `.send(built.raw` imports `deliverable`), `tests/integration/test_mail_db.py`
  (`test_a_message_to_an_invalid_address_is_recorded_and_never_sent`: real DB, real field encryption, the dispatch
  job end to end).
- `CONTEXT.md` mail section: one bullet.

## Tradeoffs

- **Mixed recipient lists.** The client sender has exactly one recipient today (the trip's client), so "drop only the
  `.invalid` ones" lives in `deliverable()` and is unit-tested; when a send path gains several recipients, the MIME
  `To` header must be built from the filtered list too (compose takes one address today).
- **`failed` vs a new status.** A `failed` message shows the failed chip and "Edit and send again" in the trip's
  Messages tab. A dedicated `not_sent` status would read better but needs a crm-NNN migration (status CHECK and
  `crm.enforce_status_transition`); the brief asked for no migration. The failure code tells the two apart.
- **Portal code mail.** The sealed code stays in `otp_challenge.code_enc` until the 24 h purge
  (`crm_stage_date_rule`), like the other failure paths (`code_expired`); the Dev echo is unchanged.

## Should know

- The review cleanup step's `meta.outbound_mail` tally (root repo `tools/uat/scenarios/crm/cleanup.cjs`) now reads
  `failed:invalid_tld` for those mails instead of `sent`; nothing bounces into the connected Gmail mailbox.
- R06 asserts the **proposal** status `sent` (not the message), so it is unaffected.
- The sender table (`app/mail/senders.py`) does not change: no send path was added or removed.
