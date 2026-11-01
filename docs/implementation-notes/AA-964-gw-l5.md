# AA-964 lane gw-l5: the CRM gateway on the shared core (S263 gateway plan F1 + F2)

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.F (F1, F2) and §2 (lane L5). Builds on the
AA-964 gateway (`AA-964.md`). Two PRs in this repo, MIG-4 order (crm-037 is a contract file):

1. `api/migrations/040_crm_llm_stage_profile.sql` + `test_crm040_schema.py` + the platform-table rows of
   `test_crm002_tenancy.py` + the CONTEXT.md migration row (migration only).
2. The code (`api/app/llm/*`, `app/routers/ops.py` status type, `crm-040` in `REQUIRED_SCHEMA_VERSIONS`), merged
   after crm-040 is applied on Dev.

A root PR (DM-7.1 retention rows for the two new tables; `crm.ops.v1` 1.7 status enum) merges before the Infra copy.

## Decisions

- **Prices do not come from the CIS contract view.** The plan (F1, T-10) says "prices via the CIS contract view per
  ADR 0008a". ADR 0008a decision 1 says `crm_app` gets nothing outside `crm` ("no grants in either direction"),
  CONTEXT.md and `tests/unit/test_aa_llm_copy.py` forbid naming `catalog.v_llm_gateway_config_v1`, and that view
  only serves `tp_*` stages to the `tripplanner` role (CIS 323). Reading it would need a CIS grant + an ADR 0008a
  amendment, both outside this lane. Instead the CRM route row carries the **catalog price** (copied from the CIS
  catalog when the row is written, `pricing_mode catalog`), and the shared core's `pricing.quote` is the safe fallback
  chain: `override` (the `CRM_LLM_USD_PER_MTOK_*` prices of `CRM_LLM_MODEL_ID`) > `catalog` (route row) >
  `code_table` (core table) > `estimate` (Sonnet list price, never 0). A live sync of the price needs a CIS HTTP
  contract (follow-up) or the tech lead's T-10 with an ADR 0008a amendment.
- **One contract migration, crm-040.** Widening the `crm.llm_call` outcome CHECK is contract (ADR 0017 d3), so the
  two new platform tables go in the same file (no second key). Key 040: 036..039 are the CRM-28 lanes'.
- **Profile tables are platform configuration** (no tenant column, no RLS, like `aa_catalog_state`): AA owns the
  route and the kill switch for every agency; the agency's own switch stays `tenant_settings.ai_enabled`. `crm_app`
  has SELECT only; a stage is switched off or rerouted by a forward migration or a DB Migrate run-script as the owner
  (a `crm.ops.v1` write route + the CIS toggle is a follow-up).
- **Route shape** follows the CIS contract view (one row per position, 0 = primary) rather than CIS's
  `fallback_model_ids` array, so an entry can be switched off and carries its own family and prices. `provider` is
  CHECKed to `bedrock` (ADR 0008a §5: Bedrock only).
- **Seed**: `crm_draft_reply` writer, active, 2 attempts, 30 s; Haiku 4.5 then Sonnet 4.5 (`us.` inference profiles):
  the two ids and list prices the vendored core already knows (`BEDROCK_HAIKU`, `BEDROCK_SONNET`, 1/5 and 3/15 USD per
  MTok), both maker `anthropic` (writer routes list no other family, ADR 0009 §5). Which models the CRM uses for real
  is still the T-12 / F4 decision; the tech lead can switch position 1 off.
- **Order of the checks**: the profile (kill switch) comes first, before the rate cap, the stub check, the budget lock
  and the scrub: it is free and a switched-off stage should not use a rate token or a guardrail unit.
- **Fail closed**: no profile `stage_unrouted`, a profile that cannot be read `profile_unavailable` (savepoint, so the
  refusal row can still be written), another role `profile_mismatch`, `is_active` false `stage_disabled`.
- **Shared core use** (F1): `errors.classify` (the Bedrock provider classifies the whole exception, never storing the
  message: `ProviderCallError.llm_error_class`), `ledger.failed_outcome` (status refused for access / credit /
  throttle, else error/other), `ledger.derive_status` (truncated), `budget.decide` (cap + unreadable), `pricing.quote`
  and `pricing.catalog_rates`. The row's `error_class` is now the core class (`throttled`, `other`, ...) instead of the
  AWS code; the AWS code stays in the warning log line `crm_llm_attempt_failed ... kind=<code>`.
- **Family = maker** (ADR 0009 §3): `app.llm.profiles.family_of` (`us.anthropic.claude-...` -> `anthropic`), the route
  row's `family` wins; the guardrail row's family is `amazon` (was `guardrails`).
- **Gateway result vs ledger row**: rows carry the five ledger v2 statuses; the gateway result (the draft API) keeps
  `ok | refused | error`: `refused` when every attempt was refused (code = the account-level class), else `error` with
  the class that ended the route (`truncated`, `other`, `deadline`, `budget`, ...). A truncated reply is never
  returned as text (was: returned with `truncated: true`); the draft falls back to "reply by hand".
- **Stub test switches**: `[stub:truncate]` and `[stub:fail-first]` inside a prompt make the stub cut its reply or
  throttle the first route entry, so the orchestrator can check truncated rows and a second attempt on Dev at $0. Only
  the stub reads them; it never runs next to a real model (`scrubber_unavailable` check, factory).
- **Budget between attempts**: a billed failure (truncated / billed_unusable) adds its cost to the month seen by the
  loop; once `budget.decide` says no, the route stops (`budget`), so a fallback never spends past 100 %.

## Changed (vs. the plan text)

- No read of the CIS contract view (see the first decision).
- The plan's `crm_draft` stage is `crm_draft_reply` (the stage name AA-964 already uses).
- `app/routers/ops.py` changed too (outside `app/llm/*`): `/llm/calls` must serve the new statuses or the response
  model would reject a truncated row (crm.ops.v1 1.7, root contract in the root PR).
- `BedrockConverseProvider` no longer requires prices: without them the core prices from its table or estimate
  (never 0). `app.settings` still refuses `CRM_LLM_PROVIDER=bedrock` without both prices (unchanged).
- `model_family()` (returned `claude-haiku`) is replaced by `profiles.family_of()` (returns the maker).

## Tradeoffs

- The profile is read on every call (one small SELECT in the call's transaction) instead of a process cache: a switch
  takes effect on the next call with no cache invalidation, for a cost that is nothing next to a model call.
- The route deadline is checked before each new attempt only; a single slow attempt is bounded by the Bedrock client
  timeout (30 s read), not cut mid-call.
- Prices on the route row can go stale against the CIS catalog; the core fallback never prices at 0 and the CRM's
  per-agency budget still caps the spend. Live sync is the follow-up.

## Should know

- **Key crm-037, not crm-040** (S263 review fix): Partner's Migration preflight takes only main max + 1 for a new
  file, and DB Migrate applies crm- files in number order with no gap. The only parallel CRM lane this session
  (AA-958 messaging) takes crm-036, so 037..039 stayed free and crm-040 would never land or apply. This PR merges
  after the crm-036 PR; if that one is not on main first, renumber to 036 (`git grep -n '037_\|crm-037\|crm037'`).
- Apply order: crm-040 on Dev **before** the code PR deploys (the code reads `crm.llm_stage_profile` and may write
  `truncated`; on the old schema every call would refuse `profile_unavailable`).
- CIS Calls tab maps an unknown CRM status to `error` today (`ai_gateway_calls.py` `SOURCE_STATUSES["crm"]`): add
  `truncated` / `billed_unusable` there (follow-up, CIS lane).
- Tests: `api/tests/unit/test_aa964_gateway_core.py` (F1/F2 unit), updated `test_aa964_crm_ai.py` /
  `test_llm_gateway.py`, `api/tests/integration/test_crm040_schema.py`, `api/tests/integration/
  test_aa964_gateway_profiles.py` (real PostgreSQL: seeded route as anthropic, kill switch, fallback, truncated).
- Unit suite as ci.yml: 2835 passed, TOTAL 90.97 %.
