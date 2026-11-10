# AA-1039 follow-up — notice only by default, one-click "Move to draft", opt-in automatic drafts (S276, Content studio)

Tech lead decision S276 (root ADR 0019 d20 amended): a tour retirement no longer changes an agency's WordPress site by
default. CIS (AA-CIS-App, same issue) writes notice-only flags, a `tour.pages_to_review` bell row and new routes; this
repo shows them in the Content studio.

## Decisions

- **Publish section beside "Put back online"** (`RetiredPagesList`, anchor `#retired-pages`), same shape and kit parts
  as `PutBackList` (Section, Badge, Button, Toast). Facebook posts are listed too, with an instruction instead of a
  button (CIS has no draft action for them; nothing is deleted on Facebook).
- **Bell**: `ReviewPagesNote` for `tour.pages_to_review`, one "Move to draft" per page, the outcome replaces the
  button (like `PutBackNote`). The sentence is built client-side from `tour_name` and the page count so it is in the
  UI language (the API's `title` / `message` are English only).
- **Settings card** (`TourRetirementSettingsCard`) on Account › Settings between the account and session cards:
  a kit Checkbox, the days Field + Input (only while the box is ticked), Save. Read-only for anyone but the
  `owner` (`useStudioRole`); CIS refuses the PATCH for the others anyway. Days validated client-side with the bounds
  the API returns (`min_days` / `max_days`), so a later bound change needs no web change.
- **The card hides itself when the read fails** (503 before CIS 395 is applied, a refused read): the settings page
  stays usable on a CIS that does not have the route yet.
- React-query keys: `["publish-log", "retired-pages"]` (refreshed by every publish-log invalidation),
  `["account", "tour-retirement"]` (set from the PATCH answer).

## Changed

- New: `retiredPagesApi.ts`, `retiredPages.messages.ts` (EN + VI), `RetiredPagesList.tsx`,
  `TourRetirementSettingsCard.tsx`, `tests/studio/retiredPages.test.ts`.
- `NotificationsBell.tsx` routes `tour.pages_to_review` to `ReviewPagesNote`; `PlaceholderTabs.tsx` (SettingsTab)
  mounts the card; `content/publish/page.tsx` mounts the section.

## Tradeoffs

- The CIS refusal sentences (404/409/422/502) are shown as the API sends them (English), like Put back online; the
  success outcomes are translated.
- No new BFF code: the tenant proxy already forwards GET / POST / PATCH.

## Should know

- Ships after the CIS code PR (the routes) is deployed; before that the section, the bell row and the card simply do
  not appear (empty / failed reads render nothing).
- Wording rule (tech lead): never say Adventure Asia will change the agency's site unless automatic drafts are on; the
  tests assert the bell copy has no "will be moved".
