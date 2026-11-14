"""AA-1096 S280 UX fix round (crm.ops.v1 1.15): the newest decision wins on the CRM settings registry, as on CIS.

- The list carries ``approved_version`` (the approved platform version in force, 0 = none: a change under an env
  override names it, where the resolved ``version`` is None) and ``scheduled`` (approved platform rows dated ahead).
- An approval supersedes with the CIS rule (in force now: every other approved row of its scope, scheduled ones too;
  future-dated: the schedules dated at or after it) and answers the versions it superseded and the scheduled ones it
  cancelled, which the CIS relay writes into its audit row.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest

from app import platform_settings as ps
from app.observability import ops_alerts
from app.platform_settings import service
from app.routers.ops_settings import SettingDecisionOut, SettingsOut
from tests.unit.fakes import ScriptedConn

VID = uuid.UUID("00000000-0000-4000-8000-0000000001c3")
NOW = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(days=3)
REASON = "zz a reason long enough for the check"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ps.configure_env({})
    ps.invalidate()
    ps.READER._snapshot = None
    ops_alerts.PENDING.drain()
    yield
    ps.configure_env({})
    ps.invalidate()
    ps.READER._snapshot = None
    ops_alerts.PENDING.drain()


def _row(**over: Any) -> dict[str, Any]:
    value = over.pop("value", 4)
    row = {
        "id": VID,
        "key": ps.INVOICE_REMINDER_MAX,
        "tenant_id": None,
        "value_text": json.dumps(value),
        "version": 3,
        "status": "approved",
        "effective_from": NOW,
        "proposed_by": "platform:zz-staff",
        "proposed_at": NOW,
        "reason": REASON,
        "evidence_ref": None,
        "self_approved": False,
        "decided_by": "platform:zz-other",
        "decided_at": NOW,
        "decision_note": None,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_the_list_names_the_approved_version_and_the_scheduled_changes() -> None:
    ps.configure_env({"CRM_LLM_MAX_TOKENS": "900"})  # an env override: the resolved version is None
    conn = ScriptedConn()
    conn.on(
        "v.tenant_id IS NULL AND v.effective_from <= now()",
        [{"key": ps.AI_DRAFT_MAX_TOKENS, "version": 2}, {"key": ps.INVOICE_REMINDER_MAX, "version": 5}],
    )
    conn.on(
        "v.tenant_id IS NULL AND v.effective_from > now()",
        [{"key": ps.INVOICE_REMINDER_MAX, "tenant_id": None, "value_text": "6", "version": 6, "effective_from": LATER}],
    )
    out = await service.list_settings(conn)
    by_key = {r["key"]: r for r in out["settings"]}
    tokens = by_key[ps.AI_DRAFT_MAX_TOKENS]
    assert tokens["overridden_by_env"] and tokens["version"] is None and tokens["approved_version"] == 2
    assert by_key[ps.INVOICE_REMINDER_MAX]["scheduled"] == [
        {"tenant_id": None, "value": 6, "version": 6, "effective_from": LATER.isoformat()}
    ]
    assert by_key[ps.RATE_AI]["approved_version"] == 0 and by_key[ps.RATE_AI]["scheduled"] == []
    SettingsOut.model_validate(out)  # the response model (extra="forbid") names both


def _decision_conn(superseded: list[dict[str, Any]], effective: dt.datetime) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.setting_value v WHERE v.id = $1", _row(status="proposed", decided_by=None, decided_at=None,
                                                             effective_from=effective))  # fmt: skip
    conn.on("UPDATE crm.setting_value AS v SET status", lambda *a: _row(decided_by=a[2], effective_from=effective))
    conn.on("UPDATE crm.setting_value SET status = 'superseded'", superseded)
    return conn


@pytest.mark.asyncio
async def test_an_approval_in_force_supersedes_the_schedules_and_names_them() -> None:
    conn = _decision_conn([{"version": 2, "scheduled": False}, {"version": 4, "scheduled": True}], NOW)
    out = await service.decide(conn, VID, staff_sub="zz-other", approve=True)
    (args,) = conn.sql("UPDATE crm.setting_value SET status = 'superseded'")
    flat = next(sql for sql, _ in conn.calls if "SET status = 'superseded'" in sql)
    assert "($4::timestamptz <= now() OR effective_from >= $4::timestamptz)" in flat  # the CIS rule
    assert "RETURNING version" in flat
    assert args == (ps.INVOICE_REMINDER_MAX, None, VID, NOW)
    assert out["superseded_versions"] == [2, 4] and out["cancelled_scheduled_versions"] == [4]
    SettingDecisionOut.model_validate(out)


@pytest.mark.asyncio
async def test_an_approval_that_supersedes_nothing_names_nothing_and_a_rejection_supersedes_nothing() -> None:
    conn = _decision_conn([], LATER)
    out = await service.decide(conn, VID, staff_sub="zz-other", approve=True)
    assert "superseded_versions" not in out and "cancelled_scheduled_versions" not in out
    rejected = _decision_conn([{"version": 2, "scheduled": False}], NOW)
    rejected.on("UPDATE crm.setting_value AS v SET status", lambda *a: _row(status="rejected", decided_by=a[2]))
    out = await service.decide(rejected, VID, staff_sub="zz-other", approve=False, note="not this quarter")
    assert out["status"] == "rejected" and not rejected.sql("SET status = 'superseded'")
    assert "superseded_versions" not in out
