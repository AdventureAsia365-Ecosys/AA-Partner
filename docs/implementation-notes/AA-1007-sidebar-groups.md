# AA-1007 (sidebar lane, S264): collapsible nav groups in the Content studio and CRM web sidebars

The tech lead asked for foldable sidebar groups in every app (S264). The behaviour is built once in the UI kit core
(AA-CIS-App `frontend/app/_kit/core`: `NavGroup.tsx`, `useNavGroups` in `useSidebar.ts`, the store at the end of
`sidebarStore.ts`; see that repo's `docs/implementation-notes/AA-1007-sidebar-groups.md`) and reaches this repo through
the Kit Sync bot. This repo only wires it, in both apps.

## Decisions

- **Content studio** (`portal/app/_kit/local/PortalSidebar.tsx`, scope `portal`): Tours, Brand, Create, Content,
  Account and Partner CRM fold; Overview has no title and stays open. The rail is a desktop state only
  (`(min-width: 900px)`, as in `globals.css`); below that the drawer always shows the headers. The headers carry
  `data-pshell-label` so the collapsed-rail CSS hides them before hydration too.
- The portal item counts (tours available, my tours) are sizes, not work waiting, so a folded Tours header does not
  repeat them (the kit `badge` is for counts to act on, like the CIS Review Queue).
- **CRM web** (`web/app/_kit/local/CrmShell.tsx`, scope `crm-web`): CRM, Money, Grow and Content studio fold; Home and
  Settings have no title and stay open. Group ids are the `lib/nav.ts` group keys (stable, not the translated label).
  No icon rail in the CRM, so `rail` is unset.
- The group of the current page is open on every arrival; other groups are folded until the user opens them.

## Changed

- `portal/app/_kit/core/*`, `web/app/_kit/core/*`: kit core copies (Kit Sync; the first commit of the branch is a local
  try-out copy, replaced by the bot's `bot/kit-sync` PR before this PR merges).
- `portal/app/_kit/index.ts`, `web/app/_kit/index.ts`: export `NavGroup`, `useNavGroups` (+ `navGroupId` for the portal).
- `PortalSidebar.tsx`, `CrmShell.tsx`: groups through the kit `NavGroup`; the local group title styles are gone (one
  `no-raw-aa-var` suppression less in each app).
- Tests: `web/tests/crmNavGroups.test.ts` (jsdom: active group open, others folded, stored choice, fold of the active
  group); the portal smoke (`e2e/smoke-portal/portal-smoke.spec.ts`) unfolds Partner CRM before checking its link.

## Tradeoffs

- The CRM web does not render the kit `I18nProvider`, so the header tooltip ("Show Money") stays English in the
  Vietnamese CRM; the visible group titles come from the CRM dictionary as before.

## Should know

- The portal group ids are slugs of the English labels in `portalNav.tsx`; renaming one resets its saved fold.
