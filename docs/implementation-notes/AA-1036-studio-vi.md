# AA-1036 — R3 lane: the Content studio in English and Vietnamese (+ AA-1007 kit-lint leftovers)

Follows `AA-1036.md` (slice 1) and `AA-1036-r3-cutover.md` (slice 2). Root ADR 0016 (UI kit), owner rule S249
(English by default, Vietnamese only when the person picks it), memory `pages-default-english`.

## Decisions

- **One mechanism, one setting.** The studio uses the kit i18n (`app/_kit/core/i18n.ts`, `useI18n.ts`): one
  `*.messages.ts` per area next to the code, `defineMessages(en, vi)` (tsc refuses a missing or extra key). The
  language is the app's existing setting, the CRM's httpOnly cookie `aa_crm_locale` (`lib/i18n.ts`): no second cookie,
  no per-page choice. Because the kit's own `createLangStore` reads a script-readable cookie, the studio gets a fixed
  store (`lib/studio/lang.ts` `studioLangStore`): `app/(studio)/layout.tsx` resolves the language per request
  (`serverContext()`), `app/_kit/local/studio/StudioLang.tsx` wraps the kit `I18nProvider` around every studio page,
  so kit core components (LoadingScreen, DataTable, Field…) follow it too.
- **The switch** is the CRM's `components/LocaleSwitch.tsx` (same form POST to `/prefs/locale`, same cookie), placed in
  the studio's account menu (not the top bar: the 390 px top bar has no room). Choosing reloads the page.
- **English copy unchanged.** Every English string moved verbatim into its catalogue (the studio smoke and the API
  guards read English text). Two stale sentences were corrected while moving: Integrations no longer mentions "your API
  key", Account settings' contact dialog no longer says "API keys" (both removed in slice 3).
- **Pure helpers take the language** (`lang = "en"` default, so their unit tests keep pinning English):
  `lib/studio/{account,facts,members,myContent,tours,masterNotice,crmIdentity}.ts`, `lib/studio/format.ts` (dates,
  months, counts), `channelName`, `servedTitle` / `publishClosedLabel` / `afterUnpublishSentence`, `watchPiece` /
  `watchTourRewrite`. Their words live in `lib/studio/lib.messages.ts` / `crmIdentity.messages.ts`.
- **Long documents are structured, not JSX.** The draft legal pages (`legal/privacy/privacy.messages.ts`,
  `legal/partner-terms/terms.messages.ts`, model `_legal/legalDoc.ts`) and the Facebook guide
  (`help/facebook/facebookGuide.messages.ts`) are `{ en, vi }` documents of paragraphs, lists and runs (plain, bold,
  code, links); the pages render them. The Vietnamese legal text is a faithful translation for reading, and the page
  says the English version applies (`legal.messages.ts` `translationNote`). Meta's button and menu names stay English
  in the Vietnamese guide (Meta's developer tools show them in English).
- **Guard**: `web/tests/studio/studioI18n.test.ts` (vitest, TypeScript compiler parse): (1) every studio catalogue
  (`app/(studio)`, `app/_kit/local/studio`, `lib/studio`) has a non-empty Vietnamese message of the same kind per
  English key and no `AA-123` id; structured documents have the same shape in both languages and no English
  placeholder left in the Vietnamese legal text; (2) no studio `.tsx` keeps hard-coded English: JSX text, a text
  attribute (`title`, `placeholder`, `aria-label`, `label`…) or an English-looking literal inside JSX (keys are
  camelCase, so `t("key")` passes; comparisons, CSS in `<style>`, acronyms and a short allow-list of names pass);
  (3) the language store: English by default, the CRM's cookie, `vi` saves `vi-VN`.
- **AA-1007 leftovers (kit-lint)**: helpers left the legacy `ui.tsx` (`lib/studio/format.ts`); the studio reads kit
  `K` / `sans` / `mono` / `serif` instead of `T`; pages use kit `PageHeader`, `Section`, `Button`, `LinkButton`,
  `IconButton`, `ListButton`, `Badge`, `EmptyState`, `LoadingScreen`, `Spinner`, `Eyebrow`; a kit-built
  `SectionSkeleton` replaces `CardSkeleton`. `ui.tsx` keeps only what has no kit twin yet (see Should know). The kit
  barrel (`app/_kit/index.ts`, app-local) now also exports `Section` and the i18n API. No kit core file changed.
- **Errors of the studio's own helpers (review fix pass)**: `reviewListApi.ts`, `catalogApi.ts` and
  `marketplaceApi.ts` no longer throw English sentences. They throw a `StudioError` (`_components/studioError.ts`)
  with a key of `errors.messages.ts` and the HTTP status, plus the API's `detail` when it sent a sentence. The kit's
  `ApiError` (`app/_kit/local/studio/api.ts`, portal-owned) now carries `detail` separately; its message is
  `HTTP <status>` when the API sent none. Components show every error through `plainError(e, fallback, lang)`
  (moved from `ServedBadge.tsx`, re-exported there): a key in the UI language, the API's sentence unchanged, a
  browser `TypeError` ("Failed to fetch") as the component's fallback. `startRewrites(ids, lang)` takes the language
  like the `lib/studio` helpers. The unused English `READY_STATE_META` and `channelLabel` were deleted.
- **One catalogue of channel names**: the Slate tab strip renders `channelName(key, lang)`; `CHANNEL_TABS` keeps
  only key and group. The command palette passes `lang` to `channelName` / `pieceTitleOrTour` too, and the writer
  panel's export title to `pieceTitle`.

## Changed (vs. the lane brief)

- Suppressions (`web/eslint-suppressions.json`): **227 -> 164** in total, **studio 127 -> 64**. By rule (studio):
  `no-legacy-kit` 33 -> 7, `no-hand-styled-button` 52 -> 17, `no-raw-aa-var` 20 -> 20, `no-hex-colour` 16 -> 16,
  `no-class-name` 3 -> 3, `no-raw-main` 3 -> 3. No entry grew, no new entry. Studio files still importing the legacy
  `ui.tsx`: 32 -> 7.
- The CRM's `LocaleSwitch` is reused as is (4 options: three English date variants and Vietnamese), so the studio
  switch also sets the date style, like the CRM's.

## Tradeoffs

- Not 0 suppressions. The rest needs kit core work or a design decision, not a lane-local move: the studio shell
  (`app/_kit/local/studio/*`: its icon buttons are shown / hidden by `data-pshell` CSS that a kit button's inline
  `display` would override; the sidebar's navy `--aa-side-*` tokens are not in `K`), the dark plan card
  (`T.onSolid`, `--aa-side-bg`), `StickyBar` (sticky card header), `CtaLink` (a link styled as the primary button: the
  kit has no anchor button), `ProgressBar`, the auto-growing `Field`, and the Slate's tab strip. Moving these into a
  new studio file would only rename the legacy layer.
- Kit `Button` secondary is the gold outline (the studio's was a neutral card button), so some studio buttons look
  like the CIS admin's now. That is the kit look (ADR 0016); no layout change.
- Text that comes from APIs is shown as sent (English): goal names and descriptions, notification titles and
  messages, the live writer's step labels, activity labels, angle text and generated content. Translating them needs
  CIS keys (follow-up), not studio strings.
- Exported files keep English column headers (`my-catalog.csv/.xlsx`): a stable format for spreadsheets and imports.
- Mail subjects to Adventure Asia stay English (they go to AA staff).

## Should know

- Follow-ups: (1) kit core (CIS canonical, then sync): an anchor variant of `Button` (for `CtaLink`), a `ProgressBar`,
  a sticky section header, sidebar tokens in `K`, and `app/(studio)/portal/_components/ui` in `LEGACY_MODULES` (drops
  the local override in `web/eslint.config.mjs`); (2) CIS: language keys for goal names, step labels, notification
  titles and activity labels; (3) the legal drafts and the privacy notice still describe the API key (slice 3 removed
  it): a copy change for counsel, not this lane.
- `api/tests/unit/test_studio_guards.py` now reads the legal pages' English section "ai" from
  `legal/*/<name>.messages.ts` (the providers and the writing fallback are still named there).
- Screenshots after deploy (orchestrator): EN and VI of `/portal/dashboard`, `/portal/tours`, `/portal/tours/mine`,
  `/portal/create/social`, `/portal/content`, `/portal/content/publish`, `/portal/brand`, `/portal/facts`,
  `/portal/account/billing`, `/portal/account/settings`, `/portal/account/members`, `/portal/account/activity`,
  `/portal/account/integrations`, `/portal/help/facebook`, `/legal/privacy`, `/legal/partner-terms` (switch: account
  menu > "Language and dates" > Tiếng Việt; back to English the same way).
- Guard 2b (`tests/studio/studioI18n.test.ts`): `.ts` helper modules under the studio folders and `lib/studio` may
  not throw (or pass to `Error` / `ApiError` / `StudioError` / `plainError`) an English-looking literal, and no
  studio file may give a text property (`label`, `title`, `message`…) an English-looking literal in an object
  literal. `*-server.ts` modules answer the BFF, not a person, and are not scanned. Run against the pre-fix files it
  reports every string the reviewers listed.
