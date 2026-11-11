# AA-1069 / AA-1070, lane l25: E2E Dev back to green

The E2E Dev workflow (`e2e-dev.yml`, Playwright on the Dev CRM web) went red on run 42960719466 (11/11, head
4a6bfb75): 7 of 10 specs failed. The last green run was 31/10 (head e4cdcfce). This note covers both issues: the
travel ops and service catalogue releases were the first full runs after the regression, not its cause.

## Decisions

- **Root cause 1 (six specs: slice1, slice2-m1m2, lifecycle-thin, aa-lines, proposal-builder, client-proposal).**
  Every one of them stops on `getByTestId("intake-dest-<country>")` on the public inquiry form. The kit-only web
  (05/11, CRM and Content studio shells on the kit ShellFrame) moved the destination chips to the kit `ChipButton` and
  kept `data-testid={...}` on it. The kit button family builds its DOM props with `domProps()`, which spreads the
  native props first and then sets `"data-testid": testId`, so a plain `data-testid` attribute is overwritten with
  `undefined`. Confirmed on Dev: the server-rendered form at the client host has the nine chips with no test id.
  The fix is the `testId` prop at the nine call sites the scanner found (inquiry form destinations, planning form
  multi-select chips, the board's bulk move menu, the ops release withdraw button, the builder's day picker, the
  profit export, the access log actor chips, the Content studio angle goals). The app fix is the right layer: the
  test ids were dead for every reader, not only the specs.
- **Root cause 2 (reports.spec).** The reports page itself rendered (screenshots at 1440/390, light/dark); the last
  step opened `${PARTNER}/` and waited for `crm-home-tiles`. Since the R3 cut-over the Dev web runs with
  `WEB_CONTENT_HOME=true`, so the bare "/" answers 307 to the Content studio dashboard; the CRM home is `/home`
  (`web/app/(crm)/home/page.tsx`). Intended change, so the spec moves, not the app.
- **slice2-m1m2's "Target page closed" at `pipelineSettings`** is the `finally` block running after the test timeout
  that root cause 1 caused; nothing separate to fix.
- **Kit core untouched.** Making `domProps()` keep a native `data-testid` would be the deeper fix, but the kit core is
  edited only in the AA-CIS-App canonical copy (ADR 0016). A follow-up for the CIS kit is listed in the lane report.

## Changed

- `web/components/**` + the studio angle wizard: `data-testid=` -> `testId=` on kit buttons (9 sites).
- `web/tests/kitButtonTestIds.test.ts` (new guard): parses every runtime `.tsx` with the TypeScript compiler, finds the
  kit components whose body calls `domProps(` (read from `app/_kit/core/Buttons.tsx`, so a new kit button is covered
  without editing the test) and refuses a `data-testid` JSX attribute on any of them, aliased imports included.
- `web/tests/stageUi.test.ts`: the destination chips and the planning form's interest chips carry their test ids.
- `e2e/tests/reports.spec.ts`: Home tiles are read at `/home`.

## Tradeoffs

- The guard is static (JSX attribute on a known kit tag). A local wrapper that forwards `{...rest}` holding a
  `data-testid` into a kit button would still slip through; none exists today, and the render tests catch the two
  flows the E2E depends on most.
- The specs were not re-run locally: they need the Dev partner-pool credentials and the e2e mailbox role, which only
  the `e2e` GitHub environment holds. The later steps of the six specs have not run since 31/10, so a second,
  unrelated failure further down is possible; the static check below lowers that risk but does not remove it.

## Should know

- Static checks done on the specs: every literal and templated `getByTestId` id exists in `web/`; every
  `getByTestId(x).getByRole("button")` wrapper is still a wrapper (not a kit button carrying the id itself); every
  button name the specs match by role exists in `web/lib/i18n.ts`; no EN string the specs read was removed since
  e4cdcfce.
- Live check after merge + Deploy Web Dev: the orchestrator dispatches `e2e-dev.yml` from main.
