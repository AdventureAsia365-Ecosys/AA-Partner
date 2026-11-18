"""AA-1113 (crm-077): the CRM's own monthly cap per model connection, read from the settings snapshot. Invented
connection keys and amounts only."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import platform_settings as ps
from app.llm import connection_share


def test_the_key_is_compiled_with_an_empty_default() -> None:
    d = ps.definition(ps.AI_CONNECTION_MONTHLY_CAP)
    assert d.value_type == "json" and d.default_value == {} and d.enforced is True
    assert (d.min_value, d.max_value, d.scope_kind, d.approval) == (0, 1_000_000, "platform", "maker_checker")


def test_the_default_caps_nothing() -> None:
    assert connection_share.caps() == {}
    assert connection_share.cap_for("zz-conn") is None


def test_a_value_in_force_is_read_per_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ps, "get", lambda key, tenant_id=None: {"zz-a": 20, "zz-b": "2.5", "zz-c": 0})
    assert connection_share.caps() == {"zz-a": Decimal(20), "zz-b": Decimal("2.5"), "zz-c": Decimal(0)}
    assert connection_share.cap_for("zz-c") == Decimal(0)
    assert connection_share.cap_for("zz-missing") is None


@pytest.mark.parametrize(
    "bad",
    [[], "zz", 5, {"zz-a": -1}, {"zz-a": True}, {"zz-a": None}, {"zz-a": "lots"}, {"": 1}, {"zz-a": "nan"}],
)
def test_anything_but_an_object_of_amounts_is_unreadable(bad: object) -> None:
    with pytest.raises(connection_share.CapUnreadable):
        connection_share.parse(bad)


def test_a_failed_snapshot_read_is_unreadable_not_no_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(key: str, tenant_id: object = None) -> object:
        raise RuntimeError("zz")

    monkeypatch.setattr(ps, "get", boom)
    with pytest.raises(connection_share.CapUnreadable):
        connection_share.cap_for("zz-a")
