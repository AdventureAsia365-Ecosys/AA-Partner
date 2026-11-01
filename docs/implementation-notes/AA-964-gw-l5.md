# AA-964 lane gw-l5: the CRM gateway on the shared core (S263 gateway plan F1 + F2)

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.F (F1, F2) and §2 (lane L5). Builds on the
AA-964 gateway (`AA-964.md`). Two PRs in this repo, MIG-4 order (crm-040 is a contract file):

1. `api/migrations/040_crm_llm_stage_profile.sql` + its integration test + the CONTEXT.md rows (migration only).
2. The code (`api/app/llm/*`, `crm-040` in `REQUIRED_SCHEMA_VERSIONS`), merged after crm-040 is applied on Dev.

A root PR (DM-7.1 retention rows for the two new tables, `crm.ops.v1` 1.7 status enum) merges before the Infra copy.

## Decisions

(filled in as the slice is built; see the sections below)

## Changed

## Tradeoffs

## Should know
