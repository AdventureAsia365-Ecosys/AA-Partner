# AA-1024 — lane l22 (S271): repeated test / CI lessons become guards (copy of the AA-CIS-App guards)

The full notes are in AA-CIS-App `docs/implementation-notes/AA-1024-l22-guards.md`. This repo gets the parts whose
code exists here.

## Decisions
- **G10, dangling migration references:** `.github/scripts/migration_refs.py` (identical in AA-CIS-App, AA-Booking,
  AA-Partner, AA-TripPlanner-Web) reads the lines a PR adds and fails on `NNN_name` naming a migration of this owner
  whose file now has another number (a renumber left behind), or a file the PR removes. A line that means the old
  number (a history note) carries `old-mig-ok`. Replayed over the last few hundred main commits of each repo it found
  only history notes of a renumber (they would now need the marker), nothing else.
- **G6, rebase refusal:** `.githooks/pre-push` refuses a push while `rebase-merge` / `rebase-apply` exists
  (lesson S257). The hook works only once installed: `scripts/install-hooks.sh` once per clone.

## Changed (vs the design)
- The integration stamp of G6 (`scripts/pre-push.sh --integration`) is built in AA-CIS-App only, where the conftest
  replays every migration; it is not copied here in this lane.

## Should know
- A history note that names a renumbered migration by its old number needs `old-mig-ok` on that line.
- **G7 (copy):** `api/tests/unit/test_aa1024_sql_order_stability.py`, same rules on `api/tests/integration`. CRM keys
  are mostly `<entity>_id`, so the uuid rule rarely applies; 12 created_at-only hits frozen in `ALLOWED`.
- G10 is called from `migration_preflight.py` (refusal 6; the preflight file stays identical in the three repos).
