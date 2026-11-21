"""AA-1171 (S287 alerts catalogue P0, decision D3; crm-085) — the CRM in-app check thresholds are settings: the defaults
equal the old constants, a changed value changes what fires, and the registry (crm.ops.v1 1.17 GET /ops-checks) lists
every check with its setting keys and the values in force. No DB, invented numbers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app import platform_settings as ps
from app.jobs.kinds import overdue_after_seconds
from app.jobs.metrics import AuditSample, Sample, backlog_sample
from app.observability import ops_alerts
from app.routers import ops_checks

DEFAULTS = {
    ps.OPS_JOBS_STUCK_AFTER_S: 900,
    ps.OPS_JOBS_OVERDUE_FACTOR: 2,
    ps.OPS_AUDIT_EXPORT_LAG_MIN: 180,
    ps.OPS_AUDIT_VERIFY_LAG_H: 50,
    ps.OPS_AUDIT_PARTITION_MONTHS_AHEAD: 1,
    ps.OPS_MEMBERSHIP_FEED_STUCK_FAILURES: 10,
}


@pytest.fixture
def setting(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    values: dict[str, Any] = {}
    real = ps.get

    def get(key: str, tenant_id: Any = None) -> Any:
        return values[key] if key in values else real(key, tenant_id)

    monkeypatch.setattr(ps, "get", get)
    return values


def test_defaults_are_the_old_constants() -> None:
    for key, default in DEFAULTS.items():
        d = ps.definition(key)
        assert (d.app, d.area, d.scope_kind, d.enforced) == ("crm", "ops", "platform", True), key
        assert d.default_value == default and ps.get(key) == default, key


def test_a_changed_threshold_changes_what_fires(setting: dict[str, Any]) -> None:
    sample = Sample(oldest_queued_age_s=1000.0, overdue_running=0, running=0, due_queued_age_s=1000.0)
    audit = AuditSample(export_lag_minutes=200.0, partition_months_ahead=1, verify_lag_hours=40.0)
    assert sorted(f.check for f in ops_alerts.state_findings(sample, audit)) == ["audit_export_lag", "jobs_stuck"]
    setting.update(
        {
            ps.OPS_JOBS_STUCK_AFTER_S: 1200,
            ps.OPS_AUDIT_EXPORT_LAG_MIN: 240,
            ps.OPS_AUDIT_VERIFY_LAG_H: 30,
            ps.OPS_AUDIT_PARTITION_MONTHS_AHEAD: 2,
        }
    )
    assert sorted(f.check for f in ops_alerts.state_findings(sample, audit)) == ["audit_partitions", "audit_verify_lag"]


def test_overdue_factor_is_the_setting(setting: dict[str, Any]) -> None:
    assert overdue_after_seconds("crm_unknown", 3) == 900
    rows = [("crm_heartbeat", 70.0)]  # expected 30 s: overdue at 2 x, not at 3 x
    assert backlog_sample(None, rows).overdue_running == 1
    setting[ps.OPS_JOBS_OVERDUE_FACTOR] = 3
    assert backlog_sample(None, rows).overdue_running == 0
    assert backlog_sample(None, rows, 2.0).overdue_running == 1


def test_registry_lists_every_check_with_its_keys_and_values(setting: dict[str, Any]) -> None:
    setting[ps.OPS_AUDIT_EXPORT_LAG_MIN] = 240
    now = datetime(2026, 11, 21, tzinfo=UTC)
    body = ops_checks.registry_body({"bulk_reveal": now - timedelta(hours=2)}, now)
    assert body["app"] == "crm" and body["version"] == 1
    checks = {c["key"]: c for c in body["checks"]}
    assert set(checks) == set(ops_alerts.CHECKS)
    lag = checks["audit_export_lag"]["thresholds"]
    assert lag == [
        {
            "setting": ps.OPS_AUDIT_EXPORT_LAG_MIN,
            "value": 240,
            "unit": "minutes",
            "editable_via": "settings",
            "where": ops_checks.WHERE,
            "label": ps.definition(ps.OPS_AUDIT_EXPORT_LAG_MIN).label,
        }
    ]
    assert checks["bulk_reveal"]["kind"] == "event" and checks["bulk_reveal"]["thresholds"][0]["editable_via"] == "none"
    assert checks["bulk_reveal"]["last_alert_at"] == (now - timedelta(hours=2)).isoformat()
    assert checks["jobs_stuck"]["kind"] == "state" and checks["jobs_stuck"]["last_alert_at"] is None
    assert all(k in ps.keys() for keys in ops_alerts.THRESHOLD_KEYS.values() for k in keys)
