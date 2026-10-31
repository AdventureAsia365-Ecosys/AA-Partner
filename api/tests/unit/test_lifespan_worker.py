"""The app lifespan starts the job worker (and the backlog metrics sampler) only when enabled, and stops them on
shutdown."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.jobs.kinds import DEPLOY_GATE_EXEMPT
from app.settings import Settings


class FakeWorker:
    instances: list[FakeWorker] = []

    def __init__(
        self,
        pool: Any,
        handlers: dict,
        *,
        recurring: tuple = (),
        hooks: tuple = (),
        no_release: frozenset = frozenset(),
    ) -> None:
        self.handlers, self.recurring, self.hooks, self.no_release = handlers, recurring, hooks, no_release
        self.started = self.stopped = False
        FakeWorker.instances.append(self)

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.started = True
        await stop.wait()
        self.stopped = True


class FakeSampler:
    instances: list[FakeSampler] = []

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self.started = self.cancelled = False
        FakeSampler.instances.append(self)

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()  # only a cancel ends it: the lifespan must cancel it, not wait for it
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.fixture(autouse=True)
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWorker.instances = []
    FakeSampler.instances = []
    monkeypatch.setattr(main_module, "Worker", FakeWorker)
    monkeypatch.setattr(main_module, "JobMetricsSampler", FakeSampler)


def _app(**kw: Any):
    settings = Settings(database_url=kw.pop("database_url", "postgresql://u@h/db"), environment="test", **kw)
    return main_module.create_app(settings, Database(settings))


def test_worker_runs_for_the_app_lifetime_when_enabled() -> None:
    with TestClient(_app(worker_enabled=True)) as client:
        assert client.get("/crm/health").status_code == 200
    (worker,) = FakeWorker.instances
    assert worker.started and worker.stopped
    assert set(worker.handlers) == {
        "crm_heartbeat",
        "crm_job_retention",
        "crm_intake_process",
        "crm_audit_export",
        "crm_audit_retention",
        "crm_placeholder_purge",
        "crm_lifecycle",
        "crm_offboard",
        "crm_dispatch",
        "crm_stage_date_rule",
        "crm_fx_fetch",
        "crm_membership_pull",
        "crm_mail_ingest",
        "crm_mail_health",
        "crm_mail_history_import",
        "crm_booking_sync",
        "crm_catalog_sync",
        "crm_studio_activation",  # AA-979
        "crm_data_export",  # AA-967
        "crm_client_import",  # AA-962
        "crm_ops_release_sync",  # AA-956
    }
    # Only the deploy-gate exempt kinds queue ahead (the heartbeat and, AA-978, the CIS membership pull); the daily
    # retention run is a due-now maintenance hook (review round: a queued-ahead daily row would block the deploy gate
    # and every DB Migrate apply).
    assert [(r.kind, r.every_seconds) for r in worker.recurring] == [
        ("crm_heartbeat", 60),
        ("crm_membership_pull", 20),
        ("crm_booking_sync", 60),  # AA-947
    ]
    assert {r.kind for r in worker.recurring} <= DEPLOY_GATE_EXEMPT
    assert [type(h).__name__ for h in worker.hooks] == [
        "DailyRetentionSchedule",
        "HourlyAuditExportSchedule",
        "DailyAuditRetentionSchedule",
        "HourlyPlaceholderPurge",
        "DailyLifecycleSchedule",
        "HourlyStageDateRule",
        "DailyFxSchedule",
        "DailyMailHealthSchedule",
        "MailIngestScan",
        "HourlyCatalogSync",
        "HourlyOpsReleaseSync",
    ]
    assert worker.no_release == frozenset()
    # AA-751: the backlog metrics sampler runs for the app lifetime too and is cancelled on shutdown.
    (sampler,) = FakeSampler.instances
    assert sampler.started and sampler.cancelled


def test_every_handled_kind_has_an_expected_run_time() -> None:
    """app/jobs/kinds.py EXPECTED_SECONDS lists exactly the kinds the worker handles (overdue-running metric), and
    every kind matches the crm.job CHECK (crm-001 job_kind_chk)."""
    from app.jobs.kinds import EXPECTED_SECONDS

    with TestClient(_app(worker_enabled=True)):
        pass
    (worker,) = FakeWorker.instances
    assert set(EXPECTED_SECONDS) == set(worker.handlers)
    assert all(re.fullmatch(r"crm_[a-z0-9_]+", kind) for kind in worker.handlers)


@pytest.mark.parametrize("kw", [{"worker_enabled": False}, {"worker_enabled": True, "database_url": None}])
def test_worker_not_started(kw: dict) -> None:
    with TestClient(_app(**kw)) as client:
        client.get("/crm/health")
    assert FakeWorker.instances == []
    assert FakeSampler.instances == []
