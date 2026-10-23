<!--
Title: "<type>: <what changed>" in English. Types: feat, fix, chore, refactor, docs, ci, test, perf.
The "PR title" check enforces this. No "[AA-123]" in the title. In the body write "Refs AA-123";
never Fixes/Closes/Resolves (Linear would close the issue).
This repo is public: no account IDs, ARNs, SG/VPC IDs, secret values or customer data in the PR or the diff.
Security and infrastructure findings go to the private ops docs, not here.
-->

## What

-

## Why

Refs AA-

## Risk and rollback

- Risk: low / medium / high (high: agency or traveller data, auth, RLS, migrations, deletes, keys, IAM)
- Blast radius: API / route class / tenancy (RLS) / migration / job runner / contract:
- Rollback: revert and redeploy the previous image, or:

## Cost

- [ ] 0 USD: no new AWS resource and no new paid API
- [ ] Adds spend: monthly estimate:

## Docs checklist

- [ ] `CONTEXT.md` updated (boundary, stack, ownership, endpoints or migrations list changed), or N/A
- [ ] ADR added or amended, and listed in `docs/adr/README.md`; cross-repo decisions go to the root ADRs; or N/A
- [ ] Runbook or how-to updated (an ops procedure changed), or N/A
- [ ] Contract or API shape change noted (contract version, CHANGELOG), or N/A
- [ ] Implementation note `docs/implementation-notes/AA-xxx.md`

## Verification

- [ ] CI green (Lint, Security Audit, Unit Tests, Integration Tests, Docker Build Check; Vendored CIS copies when touched)
- [ ] Migration `crm-NNN`: identical copy in AA-CIS-Infra `db/migrations/crm/`, applied on Dev by DB Migrate (D8 flow); base table template followed; or N/A
- [ ] No personal data in job payloads, logs or responses outside the per-role allow-list; or N/A
- [ ] After deploy: `GET /crm/health` and `/crm/health/ready` return 200, and the changed endpoint is verified live (status + shape)
- Evidence (commands, output, screenshots):
