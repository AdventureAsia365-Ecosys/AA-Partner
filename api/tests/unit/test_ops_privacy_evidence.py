"""AA-1174 (crm.ops.v1 1.18) — ``GET /crm/ops/v1/privacy-evidence``: the counts across agencies that CIS Platform ›
Compliance reads for the campaign consent and consent capture checks. Every agency is read inside ``platform_tx``
(RLS FORCE), a trashed agency is skipped, the answer carries counts and one timestamp only (no agency id or name, no
contact), and the SQL leaves erased contacts out of the violations and reads consent and suppression AT send
time. No DB."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.errors import NotFound
from app.ops import privacy_evidence as console
from app.routers import ops_privacy_evidence as route

A, B, GONE = (uuid.UUID(f"00000000-0000-4000-8000-0000000000{x}") for x in ("a1", "b2", "c3"))
LAST = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)


class Conn:
    def __init__(self, sends: dict[str, int], consent: dict[str, Any]) -> None:
        self.sends, self.consent = sends, consent
        self.args: list[Any] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.args.append(args)
        return self.sends if "dispatch_queue" in sql else self.consent


def _patch(monkeypatch: pytest.MonkeyPatch, conns: dict[uuid.UUID, Conn], opened: list[tuple[uuid.UUID, str]]) -> None:
    async def agencies(_pool: Any) -> list[Any]:
        return [SimpleNamespace(tenant_id=t) for t in (A, B, GONE)]

    @asynccontextmanager
    async def platform_tx(_pool: Any, tenant_id: uuid.UUID, *, actor: str, statuses: frozenset[str]):
        opened.append((tenant_id, actor))
        if tenant_id == GONE:
            raise NotFound("tenant_not_found")
        yield SimpleNamespace(conn=conns[tenant_id], tenant_id=tenant_id)

    monkeypatch.setattr(console.registry, "agencies", agencies)
    monkeypatch.setattr(console, "platform_tx", platform_tx)


def test_counts_across_agencies_only(monkeypatch: pytest.MonkeyPatch) -> None:
    conns = {
        A: Conn({"sent": 5, "erased": 1, "violations": 0}, {"recorded": 3, "last_at": LAST}),
        B: Conn({"sent": 2, "erased": 0, "violations": 1}, {"recorded": 0, "last_at": None}),
    }
    opened: list[tuple[uuid.UUID, str]] = []
    _patch(monkeypatch, conns, opened)
    out = asyncio.run(console.platform_privacy_evidence(object(), staff_sub="staff-1", days=30))
    assert {k: v for k, v in out.items() if k != "as_of"} == {
        "window_days": 30,
        "tenants": 2,
        "marketing_sent": 7,
        "violations": 1,
        "excluded_erased": 1,
        "consents_recorded": 3,
        "last_consent_at": LAST.isoformat(),
    }
    assert [t for t, _ in opened] == [A, B, GONE] and all(a.startswith("platform:") for _, a in opened)
    assert conns[A].args == [(30,), (30,)]
    assert str(A) not in str(out) and str(B) not in str(out)


def test_sql_reads_consent_and_suppression_at_send_time_and_skips_erased() -> None:
    sql = " ".join(console.PRIVACY_SENDS_SQL.split())
    assert "d.purpose = 'marketing' AND d.status = 'sent' AND d.gate_result = 'sent'" in sql
    assert "c.at <= d.decided_at" in sql and "s.created_at <= d.decided_at" in sql
    assert "t.traveler_id IS NOT NULL AND (c.value IS DISTINCT FROM true OR s.hit)" in sql
    assert "FILTER (WHERE t.traveler_id IS NULL)" in sql


def test_window_is_capped_and_the_model_has_counts_only() -> None:
    assert route.MAX_DAYS == 365
    fields = set(route.PrivacyEvidence.model_fields)
    assert fields == {
        "as_of",
        "window_days",
        "tenants",
        "marketing_sent",
        "violations",
        "excluded_erased",
        "consents_recorded",
        "last_consent_at",
    }
    assert route.PrivacyEvidence.model_config.get("extra") == "forbid"
