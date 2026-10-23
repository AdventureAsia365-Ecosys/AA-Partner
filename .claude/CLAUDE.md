# AA-Partner — agent instructions

- Read `CONTEXT.md` first, then root ADR 0010 (`AA-Ecosys/docs/adr/0010-aa-partner-app.md`) and ADR 0006b
  (route classes). Code that disagrees with CONTEXT.md is a question to raise, not to ignore.
- Commits: `feat|fix|chore|refactor|test|docs: <english>`, body `Refs AA-xxx`. Never `[AA-xx]` in the subject, never
  `Fixes`/`Closes` (Linear auto-closes on those).
- Subagents never merge, push to `main`, create repos, apply Terraform, run DB Migrate or write secrets.
- This repo is **public**: no AWS account ID, ARN, VPC/SG/subnet ID, Cognito pool ID or real personal data in any
  file, commit message or PR (DM-6.10; the Security Audit job fails on them).
- Migrations: `api/migrations/NNN_*.sql` (key `crm-NNN`), run as `crm_owner` by AA-CIS-Infra DB Migrate only (byte copy
  in `db/migrations/crm/`). Every tenant table follows `api/db/templates/tenant_table.sql` (the integration catalogue
  guard checks it). Never a password, LOGIN, role DDL or a grant outside schema `crm` in a migration.
- Data access: request code goes through `app.tenancy.tenant_tx()`; job rows through `app.jobs.queue`. Job payloads
  and results are ids only (`app.jobs.payload_guard`). Table/column names only from the migrations or
  `AA-Ecosys/docs/architecture/db-schema-reference.md`.
- Underscore environment variable names are set by Terraform (AA-CIS-Infra `crm_app.tf`) or the orchestrator, not by
  hand-written scripts here.
- Everything in Git is English. Keep per-issue decisions in `docs/implementation-notes/<ISSUE>.md` while working.
