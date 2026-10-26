# AA-979 P2b step 3c: the CRM web on both agency hosts

Session S252, agent Claude Code. Branch `feat/aa-979-web-per-host-origin`. Design: AA-Ecosys
`.tmp-session/s252/aa979/design.md` §2.2 and critic fix C4; orchestrator decisions Q1 (the CRM web moves to `portal.`
with `partner-dev.` kept) and Q12 (both hosts serve during the window).

## Decisions

- **Per-host public origin** (`lib/config.ts` `forHost(cfg, host)` / `requestConfig(request)`): a Host listed in
  `WEB_PARTNER_HOSTS` gets `https://<host>`; any other Host keeps `WEB_PUBLIC_ORIGIN`; never the bind address. Every
  route handler, the proxy gate and `serverContext()` take the per-request configuration, so `redirectTo`, the
  write guard, `authorizeUrl` / `callbackUrl` / `logoutUrl` need no change. Local http development is untouched.
- **Why not one origin flipped at cutover**: with one origin, sign-in on `portal.` before the flip sends the callback to
  `partner-dev.` (the OAuth transaction cookie is host-only, so the pre-cutover smoke cannot pass), and after a flip
  every `partner-dev.` POST would be 403. Per host, cutover and rollback are DNS only.
- **Content studio link** is `/portal/dashboard` (same origin) on the agency host, the absolute portal URL elsewhere.
- **No CRM agency** on the agency host -> `/portal/dashboard` (a Content-studio-only member); on `partner-dev.` the
  empty state stays.
- **Sign-out clears the studio's five `cis_*` cookies** too. The helper lives in `lib/studioCookies.ts`, not in
  `lib/session.ts`, because the portal holds a byte copy of `session.ts` (portal PR) and the two PRs must not race.

## Changed

- Nothing from the design dropped. The mail OAuth callback (`/connect/mail/callback`) stays on `partner-dev.`: the API's
  `MAIL_OAUTH_REDIRECT_BASE` is not changed in P2b (critic C4); a mailbox connect started on `portal.` finishes on
  `partner-dev.` during the window (clean-up moves it with the Google / Microsoft redirect URIs).

## Should know

- Needs AA-CIS-Infra PR-1 for the env (`WEB_PARTNER_HOSTS` = both hosts) and the partner-web callback / sign-out URLs
  of `portal.`. Without it the web behaves exactly as before (one partner host).
- Tests: `web/tests/perHostOrigin.test.ts`; `writeGuard.test.ts` / `routes.test.ts` expect the 5 extra expired cookies.
- Deploy with Deploy Web Dev (no job guard); smoke `partner-dev.` CRM as usual. `portal.` is checked pre-cutover through
  the agency distribution (`crm_edge_live_check.py --agency-cf-domain`).

## Second commit: the join pages and a Content studio invitation

- The API's accept answers `{kind: "studio", tenant_id, status}` for a Content studio invitation (API PR
  `feat/aa-979-studio-invitations-api`). `/join/confirm` then clears the invitation cookies, leaves the CRM session
  and its agency untouched (no CRM membership was created) and goes to `/join/accept?studio=activated|accepted`, which
  says "ready" or "being set up" and links the Content studio. EN/VI keys `join.studioReady`, `join.studioPending`,
  `join.toStudio`. This web change is harmless before the API PR deploys (the answer simply never has `kind`).
