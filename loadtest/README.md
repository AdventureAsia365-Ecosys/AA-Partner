# Bounded load test of the AA-Partner API (AA-969, CRM-37a)

50 synthetic agencies against **Dev**, read-only, at most 30 requests per second for at most 10 minutes, on the
shared RDS. The question it answers is design card CRM-37a's R: **a noisy agency cannot push another agency past the
SLO** (`docs/operations/slo.md`). Only the orchestrating agent or the tech lead runs it, never CI, never a lane.

| File | What |
|---|---|
| `crm_load.js` | the k6 script: scenario `calm` (49 agencies, round robin, constant rate, the SLO thresholds) and `noisy` (the first agency, from minute 2 to one minute before the end) |
| `crm_loadtest.py` | the runner: pre-checks, starts k6, re-checks the guards every 30 s, stops k6 on any abort condition |
| `get_token.py` | signs the load-test identity in (password + authenticator) and writes its access token to a 0600 file |

## Why it is safe on the shared database

- **Connections**: the load reaches the database only through the CRM API, whose pool is 3 connections per task
  (`crm_app` CONNECTION LIMIT 6). The test adds no connection of its own, so the CIS, Booking and TripPlanner budgets
  (`db/connection_budget.json` in AA-CIS-Infra) are untouched whatever the load.
- **Reads only**: every request is a `GET`, or the client search `POST` (a read). Nothing is written to any agency.
- **Synthetic agencies only**: `lt-01` .. `lt-50` (`is_synthetic`, invented names), signed in as one load-test identity
  that is a member of those 50 only.
- **Ceilings the arguments cannot raise**: 10 minutes, 30 RPS (calm + noisy), 30 noisy virtual users, 50 agencies, a
  `-dev.` https origin. k6 itself refuses the same numbers.
- **Hard stops** (checked before the start and every 30 s): `shared.job` not idle, `booking.job` backlog, the CIS
  job-lag signal (`jobs_starved` and friends: the in-app checks that replaced the CloudWatch job-lag alarm in the S263
  alert diet), a CRM `crm.job` lag, any CIS / Booking / CRM CloudWatch alarm in ALARM, the CRM not ready, an AWS read
  that fails or times out (an expired MFA session fails at once: stdin is closed), a sampler line that lacks its job
  fields or carries `jobs_error` / `ops_checks_error` (all fail closed), the calm agencies' error rate above 1 % (k6
  `abortOnFail`), and a wall clock of the duration + 2 minutes. If the runner itself fails mid-run, it stops k6
  before it exits (exit 3): k6 never keeps running unguarded.

## One-time setup (orchestrator)

1. Merge and deploy the AA-Partner PR (the seeder's `--load-test` and the fair share) and apply the AA-CIS-Infra PR
   (SLO alarms, and the `lt` / `agent` e2e entry, which adds a recipient to the e2e mailbox rule).
2. Create the load-test identity (AA-CIS-Infra, workstation, MFA; creates only the missing users):

   ```
   python3 tools/identity/crm_dev_identity.py --pool partner e2e-users --apply --write-secrets
   ```

   It prints `--member lt:agent:<sub>`: keep the `<sub>`. Add the secret `crm/dev/e2e-test-crm/lt-agent` to the
   root secrets register row set (same shape as the other e2e users).
3. Create the 50 agencies with the identity as an agent of each, inside the CRM API container (ECS Exec, the usual
   S3-mediated pattern), DRY RUN first:

   ```
   python -m app.seed --load-test 50 --load-test-sub <sub>
   python -m app.seed --apply --load-test 50 --load-test-sub <sub> --travellers
   ```

   `--travellers` stores 6 synthetic inquiries per agency through the vault (300 `crm_intake_process` jobs): wait
   until `crm.job` is idle again before step 5. Keep the printed `tenant_id` values, `lt-01` first, as a JSON list
   in a scratch file (`tenants.json`); never commit it.

## Each run

Pre-checks (all must hold; the runner checks 4-7 itself and refuses with exit 2):

1. No wave open, no DB Migrate / Migration Apply / Terraform Apply running, no deploy in progress (any repo).
2. Every job table idle by the Infra idle gate (exact, with the exempt kinds):
   `gh workflow run terraform-apply.yml -R AdventureAsia365-Ecosys/AA-CIS-Infra --ref main -f idle_gate_check=true`,
   then wait for the run: green = idle. Red = stop here.
3. A fresh token (lives 30 minutes; the runner wants the run + 5 minutes left):
   `python3 loadtest/get_token.py --out <scratch>/lt-token`
4. The AWS session is the Dev account (`--expect-account`, not written in this public repo).
5. `/crm/health/ready` is `ok`.
6. The newest CIS, Booking and CRM `ops_metrics_sampled` lines (at most 11 minutes old) show the queues idle and no
   job / database check raised.
7. No CIS / Booking / CRM CloudWatch alarm in ALARM.

Run (k6 v0.57 or newer on the PATH, or `--k6 <path>`):

```
python3 loadtest/crm_loadtest.py run --base-url https://partner-dev.<zone> --tenants-file <scratch>/tenants.json --token-file <scratch>/lt-token --expect-account <acc2 id> --duration-min 8 --calm-rps 6 --noisy-rps 12 --noisy-max-vus 12 --summary-file <scratch>/lt-summary.json
```

`precheck` instead of `run` checks everything and starts nothing. `--extra-guard "<command>"` adds a check of your
own (exit 0 = fine), e.g. a job-table probe you trust more than the sampler lines.

Exit codes: 0 every SLO threshold held; 4 k6 finished but a threshold (an SLO) failed; 3 aborted by a guard (the
reasons are printed); 2 refused before the start.

## Reading the result

The summary file is k6's JSON. The verdict for card R:

- `http_req_duration{tenant_kind:calm,group:session|agency-read|reports}` p(95) under 300 / 800 / 1500 ms;
- `calm_errors` rate < 1 %, `calm_refused` rate < 0.5 % (the fair share must not touch the calm agencies);
- `noisy_refused` > 0 is expected: the noisy agency meets its share (429 `tenant_busy` / `rate_limited`).

Compare the calm p95 of minutes 0-2 (no noisy load) with the rest using the saved Logs Insights query
`aa-crm-dev/slo/p95-by-route-group` over the two windows, and `aa-crm-dev/slo/fair-share-refusals` for who was
refused. Put the numbers (no tokens, no ids beyond `lt-NN`) in the AA-969 evidence.

## After the run

Nothing to clean: the test wrote nothing. The `lt-NN` agencies stay for the next run (synthetic; the L1 console can
offboard them when no longer wanted). Delete the scratch token file.
