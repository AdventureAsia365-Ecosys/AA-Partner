---
id: OPS-01-crm-slo
title: "AA-Partner service level objectives"
description: The CRM's SLOs (availability, p95 latency per route group, crm.job lag, error rate), how each is measured and alarmed at no extra cost, and the fair-share limits that keep one agency from pushing another past them.
type: reference
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-crm-api, aa-crm-web]
last_reviewed: 2026-11-05
review_every_days: 90
related: [ARCH-06-partner]
---

# AA-Partner service level objectives

Set by AA-969 (design v3 card CRM-37a). Dev has no SLA (root ADR 0019 d16: "test environment, no SLA"); these are the
targets the service is built and tested against, and the thresholds the alarms use. Thresholds live in two places
that must stay equal: this page and the `locals` at the top of AA-CIS-Infra `modules/environment/crm_slo.tf`.

## Objectives

| SLO | Target | Window | Measured from | Alarm / check |
|---|---|---|---|---|
| Availability | 99.5 % of requests answered without a 5xx, and at least one healthy API task | 30 days | ALB target group `HTTPCode_Target_5XX_Count` / `RequestCount`, `HealthyHostCount` | `aa-crm-<env>-no-healthy-task` (5 min, existing) |
| Error rate | 5xx below 2 % of requests in a 5-minute window (with at least 20 requests) | 5 min | the same ALB metrics | `aa-crm-<env>-api-5xx-rate`: 2 of 3 windows above 2 % |
| Latency, all routes | p95 below 1.5 s | 5 min | ALB `TargetResponseTime` p95 | `aa-crm-<env>-api-p95-latency`: 2 of 3 windows above 1.5 s |
| Latency, `session` | p95 below 300 ms | run / day | `request_completed` log lines; load test threshold | saved query `slo/p95-by-route-group` |
| Latency, `agency-read` | p95 below 800 ms | run / day | same | same |
| Latency, `reports` | p95 below 1.5 s | run / day | same | same |
| Latency, `agency-write` | p95 below 1 s | day | same | same |
| Latency, `public` | p95 below 500 ms | day | same | same |
| crm.job lag | the oldest due queued row below 120 s in 99 % of samples; never above 900 s | 5-min samples, 7 days | the `ops_metrics_sampled` line (`oldest_queued_age_s`) | in-app check `jobs_stuck` (> 900 s, e-mail to AA ops); saved query `slo/crm-job-lag` |
| Fairness | a noisy agency never moves a calm agency outside the SLOs above | load test | load test thresholds on the calm agencies | the bounded load test (`loadtest/README.md`) |

### Route groups

A route group is a set of route templates (`route` in `request_completed`, no ids, no query string):

| Group | Routes |
|---|---|
| `session` | `GET /crm/v1/me`, `GET /crm/v1/memberships` |
| `agency-read` | the other `GET /crm/v1/*` reads: pipeline, trips, tasks, travellers, duty, workflows, members, onboarding, imports |
| `reports` | `GET /crm/v1/reports/*` |
| `agency-write` | every `POST` / `PUT` / `PATCH` / `DELETE` under `/crm/v1` (uploads excluded: they scale with the file) |
| `public` | `/crm/public/v1/*` (inquiry form, client pages, traveller portal, calendar feed) |
| `staff` | `/crm/ops/v1/*`, `/crm/staff/v1/*` (AA staff only; no separate target) |

The saved query reports per `route_class` + first path segment + method; read the table with the mapping above.

## Cost

Only free AWS metrics become alarms (ALB metrics of the API target group): three alarm metrics at 0.10 USD each =
**0.30 USD per month**. No custom metric, no metric filter (both would be billed custom metrics, and the S263 alert
diet removed them), no dashboard. The saved Logs Insights queries cost nothing until run (billed per GB scanned).
The crm.job lag check runs in the API process (`app/observability/ops_alerts.py`) at no AWS cost.

## Fair share: how one agency is kept from pushing another past the SLO

The API task has 3 database connections (`crm_app` CONNECTION LIMIT 6 = 3 x 2 tasks during a rolling deploy), shared
by every agency and the job worker. Four mechanisms keep one agency's load from reaching the others:

1. **Agency slots** (`app/tenant_gate.py`): at most `CRM_TENANT_MAX_CONCURRENCY` (default 2) `tenant_tx()` blocks run
   at once per agency per task; a request that waits more than `CRM_TENANT_SLOT_WAIT_MS` (default 5 s) for its slot is
   answered **429 `tenant_busy`** with `Retry-After: 1`. The third connection always stays reachable by another agency.
   The slot is keyed by the agency only once the caller's membership was verified, so naming another agency's id in
   `X-AA-Tenant-Id` never uses that agency's slots.
2. **Bounded pool wait** (`app/tenancy.py`): a request waits at most 10 s for a pooled connection, then **503
   `db_busy`** with `Retry-After: 2` (before, the wait was unbounded).
3. **Member ceiling** (`app/auth/route_guard.py`): `RATE_LIMIT_MEMBER_PER_MINUTE` (default 600) requests per member
   per agency per minute on `/crm/v1`, then **429 `rate_limited`**.
4. **Fair job claim** (`app/jobs/queue.py`): the serial `crm.job` worker takes, among the due rows, the one whose
   agency was served least in the last 5 minutes (oldest first inside an agency), so an agency's import or message
   backlog does not hold every other agency's jobs behind it.

And for every route, the **body ceiling** (`app/bodylimit.py`): 1 MiB per request (upload routes their own cap plus 64
KiB), enforced before any route code, the class guard included.


## When an SLO is missed

- 5xx rate or no healthy task: root `docs/ops/playbooks/5xx-spike.md` (the alarm e-mail links it).
- p95 latency: root `docs/ops/incident-response.md`; run the saved query `slo/p95-by-route-group` for the window to
  find the group, then `slo/fair-share-refusals` to see whether one agency is being held back (expected) or every one
  is slow (capacity: database, KMS, the task size).
- crm.job lag: the `jobs_stuck` e-mail and root `docs/ops/playbooks/queue-stuck.md`.

## Changing a threshold

Change this page and `crm_slo.tf` in the same session (two PRs: AA-Partner docs, AA-CIS-Infra Terraform), and the
load test thresholds in `loadtest/crm_load.js` when a latency target changes.
