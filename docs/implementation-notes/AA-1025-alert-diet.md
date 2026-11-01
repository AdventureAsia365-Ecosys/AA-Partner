# AA-1025 (lane alert-diet, S263) — in-app ops checks instead of CloudWatch alarms (AA-Partner)

Decision table: root `AA-Ecosys/docs/audits/2026-11-01-S263-alerting-cost/diet.md`.

## Decisions

- One module, `app/observability/ops_alerts.py`, for the 15 CRM alarms that moved in-app: state checks from the job
  sampler read (job queue stuck / overdue, the three audit export checks, same thresholds) and events raised with
  `ops_alerts.emit()` next to the existing alarm log lines (break-glass opened / owner not mailed, ops release T-21 /
  withdrawn, membership event refused / feed stuck, bulk reveal). The log lines stay.
- E-mail through the platform alerts SNS topic (`ALERTS_TOPIC_ARN`): the topic the removed alarms used (same AA ops
  recipients), sent by the sampler in the API process, not by a job (the checks are about the worker too).
- Dedupe without a migration: a `succeeded` ledger row in `crm.job` per alert key (`crm_ops_alert:<check>:<scope>`,
  scope = the session / release / event id, member + hour for bulk reveal, else the UTC day). `INSERT ... ON CONFLICT DO
  NOTHING` on the existing unique `idempotency_key` index makes it race-free across the two API tasks; a failed publish
  deletes the row and puts an event back in the queue for the next tick. Payload ids only (`check_payload`).
- The sampler no longer publishes `AA/Jobs/Crm`; the reveal peak and the dropped forward-in mails stay on the
  `ops_metrics_sampled` log line (they never had their own alarm after the diet).

## Changed

- `app/jobs/metrics.py`: `JobMetricsSampler(pool, every_s, publisher=...)`, no boto3 CloudWatch.
- `app/jobs/queue.py`: `record_alert`, `forget_alert`.
- Review pass: `ops_alerts.flush_events`, `JobMetricsSampler.flush_events`, the lifespan's shutdown flush
  (`app/main.py`), `_Pending.__len__`.
- Emit calls: `app/support/sessions.py`, `app/ops_release/service.py` (`Notice.emit`), `app/cis/membership_feed.py`,
  `app/observability/reveal_meter.py`.

## Tradeoffs

- Events wait in memory until the next sampler tick (at most 5 minutes; bounded at 500). Review pass: the lifespan
  flushes them once more on shutdown, after the worker stopped and before the pool closes (`JobMetricsSampler.
  flush_events`, bounded at `OPS_FLUSH_ON_STOP_TIMEOUT_S` = 5 s: 20 s worker + 5 s inside the 30 s stopTimeout), and a
  tick the lifespan cancels mid-way puts its drained events back first. So a deploy, a scale-in or a task replacement
  no longer drops a break-glass or ops-release alert; only a hard kill (OOM, SIGKILL, host failure) between the event
  and the next tick does (the log line remains, and a break-glass read is also in the agency's access log).
- A durable ledger row at emit time was weighed and rejected for now: `emit` is sync and called from inside request
  and job transactions (break-glass open, ops release notice); writing `crm.job` there would tie the alert to the
  caller's transaction and add a DB round trip to the request. Revisit before prod if the hard-kill window matters.
- The sampler runs only when the worker is enabled (as before). With `WORKER_ENABLED` off, no CRM check runs.
- One e-mail per check per UTC day for state checks: a check that recovers and breaks again the same day is not mailed
  twice.

## Should know

- Needs the AA-CIS-Infra apply (`sns:Publish` on the task role, `ALERTS_TOPIC_ARN` in the task definition) and then a
  deploy; before that a finding is only logged (`ops_check_finding`).
- Coverage: `app/observability/ops_alerts.py` and `app/jobs/metrics.py` at 100 %; full unit TOTAL 91.25 % (review pass).
- Live proof: one due `crm.job` row of an unhandled kind 20 minutes old (or an `ops_alerts.emit` through a break-glass
  open on a synthetic agency) mails once and leaves one `crm_ops_alert` row.
