# AA-1039 follow-up — "Put back online" in the Content studio (option A, S275)

Joins the CIS change of the same lane (AA-CIS-App `docs/implementation-notes/AA-1039-put-back-online.md`): after a
restore, CIS never republishes a WordPress page the tour retirement moved to draft; the tenant puts each one back with
one click.

## Decisions

- **Two entry points, one call.** Publish gets a "Pages to put back online" section above Ready to Publish
  (`PutBackList`, anchor `#put-back`); the bell renders a `tour.pages_to_put_back` row as `PutBackNote` (the sentence
  links to that section, each page has its own button). Both call `putBack()` in `putBackApi.ts`:
  `POST /api/tenant/v1/publish-log/{publish_id}/put-back` through the existing tenant BFF (no new proxy route).
- **The outcome in the UI language**: the API's `message` is English only, so `outcomeText()` maps `outcome` +
  `wordpress_status` to `putBack.messages.ts` (EN default, VI). A refusal shows the API's own sentence (`plainError`).
- **Section hidden when empty or refused** (a staff view-as session gets 403 on the list read): the Published list
  stays the page's record.
- **Query key** `["publish-log", "put-back"]`: a click refreshes every `["publish-log"]` query (the Published list
  shows the page live again) and Ready to Publish (`reloadSignal`).
- **Bell button state is local**: after the click the button is replaced by the outcome line; a later click from an
  old bell entry answers "already online" from CIS (replayed).

## Changed

- New: `_components/putBack.messages.ts`, `_components/putBackApi.ts`, `_components/PutBackList.tsx`,
  `tests/studio/putBack.test.ts`.
- `_components/NotificationsBell.tsx`: `pages` on `TenantNotification`, `PutBackNote`, the new row type.
- `portal/content/publish/page.tsx`: the section.
- `CONTEXT.md`: the "Put back online" bullet.

## Tradeoffs

- The bell lists the pages written at restore time; it does not re-read the live list (one fewer request per bell
  open). A page put back from Publish still shows a button in the bell until clicked, which then answers
  "already online" without a WordPress call.

## Should know

- Kit components only (`Section`, `Button` with `testId`, `Badge`, `Toast`); no edit under `app/_kit/core`.
- Needs the CIS routes deployed first (CIS PR of the same lane); before that the list read 404s and the section stays
  hidden, the bell has no such row.
