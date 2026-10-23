# AA-Partner ADRs

Decisions that affect this app only. The cross-repo decisions behind AA-Partner (placement and runtime, identity and
route classes, the CRM's exceptions to the new-app rules, the job model) are ecosystem ADRs in the AA-Ecosys root repo
under `docs/adr/` (0010, 0006b, 0008a, 0003a and the ones they implement). An app ADR here implements one of them and
cites it. Infrastructure-only decisions live in AA-CIS-Infra.

Each ADR states its status near the top (`- **Status:** Accepted (dd/mm/yyyy, S<nnn>)`). Add every new ADR to this
table in the same PR.

| ADR | Decision | Status | Implements | Linear |
|---|---|---|---|---|
| [0001](0001-implements-root-0010.md) | Implement root ADR 0010 (repo, API skeleton, route classes, tenancy base, job runner) in this repo | Proposed (23/10/2026, S248) | root ADR 0010, 0006b, 0003a | AA-933 |

Template: the ecosystem ADR template (AA-Ecosys `docs/templates/adr.md`).
