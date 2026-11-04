# AA-1036 R3: the studio in Vietnamese in the portal UI smoke (S268, lane portal-smoke)

Spec: `e2e/smoke-portal/s268-studio-price-vi.spec.ts`, second test (the first is AA-1029, `AA-1029-portal-smoke.md`).

## Decisions

- **The real switch.** The language is changed the way a person does it: account menu > "Language and dates" (the
  CRM's `LocaleSwitch`, `data-testid="locale-select"`), which posts `/prefs/locale` and reloads the page with the
  `aa_crm_locale` cookie. The reload is awaited on `<html lang>` (`vi` / `en`, root layout).
- **One heading per page from the catalogues**: dashboard `h1` "Chào mừng trở lại, <tenant>" or "Tổng quan"
  (`dashboard.messages.ts`), Marketplace `h1` "Kho tour" (`content.messages.ts` marketTitle), Brand Identity `h2`
  "Nhận diện thương hiệu" (`brand.messages.ts` BRAND_MESSAGES title).
- **Screenshots**: `results/smoke-portal/aa1036-vi-{dashboard,tours,brand}-desktop-light.png` (whole main, as
  `portalScreenshots` does) and `...-mobile-dark.png` (390, dark after a reload, like `portalScreenshots`).
- **Back to English.** The locale the switch showed at the start (an English variant, else en-AU) is chosen again,
  then the "Brand Identity" heading and the switch value are checked. The cookie lives in this test's browser context
  only (not in the shared storage state), so other specs and the user stay English either way.

## Changed

- Nothing in the app.

## Should know

- Not run locally (no studio sign-in on disk); tsc and `--list` only. First real run: UI Smoke (portal) after the next
  Deploy Web Dev.
