# AA-964 lane gw-l5: the CRM gateway on the shared core (S263 gateway plan F1 + F2)

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.F (F1, F2) and §2 (lane L5). Builds on the
AA-964 gateway (`AA-964.md`). Two PRs in this repo, MIG-4 order (crm-037 is a contract file):

1. `api/migrations/037_crm_llm_stage_profile.sql` + its integration test + the CONTEXT.md rows (migration only).
2. The code (`api/app/llm/*`, `crm-037` in `REQUIRED_SCHEMA_VERSIONS`), merged after crm-037 is applied on Dev.

A root PR (DM-7.1 retention rows for the two new tables, `crm.ops.v1` 1.7 status enum) merges before the Infra copy.

## Decisions

(filled in as the slice is built; see the sections below)

## Changed

## Tradeoffs

## Should know

- **Key crm-037, not crm-040** (S263 review fix): Partner's Migration preflight takes only main max + 1 for a new
  file, and DB Migrate applies crm- files in number order with no gap. The only parallel CRM lane this session
  (AA-958 messaging) takes crm-036, so 037..039 stayed free and crm-040 would never land or apply. This PR merges
  after the crm-036 PR; if that one is not on main first, renumber to 036 (`git grep -n '037_\|crm-037\|crm037'`).
