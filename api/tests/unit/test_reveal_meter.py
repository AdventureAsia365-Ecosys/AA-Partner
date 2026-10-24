"""Reveals per member per hour (AA-937, design §4.4): the sliding window, the peak and the bulk-reveal warning."""

from __future__ import annotations

import logging
import uuid

import pytest

from app.observability.reveal_meter import RevealMeter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_counts_per_member_and_peak() -> None:
    clock = Clock()
    meter = RevealMeter(clock=clock)
    tenant, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert meter.peak() == 0
    assert meter.record(tenant, a) == 1
    assert meter.record(tenant, a, 2) == 3
    assert meter.record(tenant, b) == 1
    assert meter.record(uuid.uuid4(), a) == 1  # the same member id in another agency is another key
    assert meter.count(tenant, a) == 3
    assert meter.peak() == 3
    assert meter.record(tenant, a, 0) == 3  # nothing released, nothing counted


def test_the_window_slides() -> None:
    clock = Clock()
    meter = RevealMeter(window_s=3600, clock=clock)
    tenant, member = uuid.uuid4(), uuid.uuid4()
    meter.record(tenant, member, 5)
    clock.now += 1800
    meter.record(tenant, member, 2)
    assert meter.count(tenant, member) == 7
    clock.now += 1801  # the first five are now older than an hour
    assert meter.count(tenant, member) == 2
    clock.now += 1800
    assert meter.peak() == 0


def test_bulk_reveal_warns_once_per_crossing(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    meter = RevealMeter(threshold=3, clock=clock)
    tenant, member = uuid.uuid4(), uuid.uuid4()
    caplog.set_level(logging.WARNING, logger="app.observability.reveal_meter")
    meter.record(tenant, member, 2)
    assert "crm_bulk_reveal" not in caplog.text
    meter.record(tenant, member)
    meter.record(tenant, member)
    assert caplog.text.count("crm_bulk_reveal") == 1
    assert f"tenant={tenant} member={member} reveals_last_hour=3 threshold=3" in caplog.text
    clock.now += 3601  # the hour passed: a new crossing warns again
    meter.record(tenant, member, 3)
    assert caplog.text.count("crm_bulk_reveal") == 2


def test_reset() -> None:
    meter = RevealMeter(clock=Clock())
    meter.record(uuid.uuid4(), uuid.uuid4(), 4)
    meter.reset()
    assert meter.peak() == 0
