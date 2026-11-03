# AA-1027 F3 follow-ups: test-mode copy and plan state on Settings > AI

Lane l5-f3-copy of S265. Findings F2 and F3 of the S265 live check of slice F3 (root
`docs/audits/2026-11-03-S265-live-verify/f3/README.md`).

## Decisions

- **Three fields instead of one.** `GET /crm/v1/settings/ai` now returns `model_test_mode` (provider is the stub:
  the model costs nothing), `scrub_billed` (a guardrail is configured: every draft's privacy check calls Bedrock
  `ApplyGuardrail` on acc2, billed, logged as `crm_scrub` rows that count in the month's spend) and `test_mode`,
  now true only when **nothing** billable runs (stub model and no guardrail). `provider` is unchanged.
- **`scrub_billed` = `SCRUB_GUARDRAIL_ID` set** (`app.privacy.guardrails.guardrail_billed`, also used by
  `build_scrubber`). `build_gateway` picks the real scrubber when a guardrail is set or the provider is real; with a
  real provider and no guardrail the scrubber refuses every scrub (fail closed) and pays nothing, so the guardrail id
  alone decides billing.
- **Web wording.** `lib/ai.ts` `testModeNote()`: `null` on a real model; `"free"` (the old "nothing is charged"
  sentence, still true there) only when the API says `test_mode` and `scrub_billed: false`; `"scrub_billed"`
  otherwise, including an older API that sends only `test_mode` (the page never claims "free" unless the API says so).
  New strings `ai.testModeHelpScrub` and `aiDraft.testModeScrub` (EN + VI): the model is a test model and costs
  nothing; only the privacy check is billed, at a very small cost.
- **Plan state on Settings > AI (F3).** AI drafts are the only consumer of `ai_enabled` (the only `gateway.call` is
  `agency_ai.draft`), so when `ai_assist_allowed` is false the page shows a "Not on your plan" badge next to the On/Off
  status (On is no longer green then) and a status line with the trip card's exact string (`aiDraft.noPlan`, the same
  key). The owner keeps the switch and gets one more sentence: drafts start once AA support adds the assistant.

## Changed

- `agency_ai.ai_settings` / `update_ai_settings` take `scrub_billed` (keyword, default `False` so service callers
  and integration tests keep working); the router passes `guardrail_billed(settings)`.
- `test_mode` semantics: was `provider == "stub"`, now `provider == "stub" and not scrub_billed`. On Dev today
  (stub model, guardrail set) it turns false; `model_test_mode` keeps the old meaning.

## Tradeoffs

- Redefining `test_mode` instead of only adding fields makes an old web (deployed before this one) show no test-mode
  note at all on Dev rather than the wrong "nothing is charged" sentence. Either deploy order is safe.
- No exact price in the copy ("a very small cost"): the guardrail price is a code table that can change; the meter
  already shows the real spend.

## Should know

- No migration, no new env var. `CONTEXT.md` Settings > AI bullet updated.
- Checks run in the lane: unit suite with coverage (3217 passed, TOTAL 92 %; `agency_ai`, `guardrails`,
  `routers/ai` 100 %), AI integration tests on local PostgreSQL 16 (`test_crm040_ai_draft`, `test_aa964_ai_gateway`,
  `test_aa964_gateway_profiles`: 13 passed), ruff 0.16.9 check + format, web vitest (715 passed), `tsc --noEmit`,
  eslint (no new warning), gitleaks 8.30.1.
