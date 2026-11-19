"""AA-1143 (tech lead decision G3, crm-079): the CRM's copy of its connection budget shares, sent by CIS.

- :func:`app.llm.connection_share.share_for`: the copy's row wins; a connection the copy does not name, or a database
  without crm-079, falls back to the setting ``crm.ai.connection_monthly_cap_usd`` (dual-read for one release);
- the gateway's cap uses it: a copy of 8 lets a spend of 7 through and stops at 8 under the dev rules; a copy of 0
  stops the connection at once;
- :func:`check_copy` refuses a bad key, a repeated key and an amount outside 0..1000000; :func:`write_copy` replaces the
  whole copy and refuses a short reason.
Fakes only (no database); invented connection keys and amounts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app import platform_settings as ps
from app.llm import connection_cap, connection_share
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa1113_ai_paths_ledger import FakeProvider, _isolated  # noqa: F401 — autouse isolation

pytestmark = pytest.mark.asyncio

CAP_KEY = "crm.ai.connection_monthly_cap_usd"
READY = "to_regclass('crm.llm_connection_share')"
ONE = "FROM crm.llm_connection_share WHERE connection_key"
SPEND = "FROM crm.llm_usage_daily WHERE day >="
REASON = "zz test: the CIS budget save reason"


class BedrockLike(FakeProvider):
    connection_key = "bedrock"


@pytest.fixture
def setting(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    value: dict[str, Any] = {}
    real = ps.get
    monkeypatch.setattr(ps, "get", lambda key, tenant_id=None: value if key == CAP_KEY else real(key, tenant_id))
    return value


async def test_the_copy_row_wins_over_the_setting(setting: dict[str, Any]) -> None:
    setting["acc2"] = 5
    c = ScriptedConn().on(READY, True).on(ONE, Decimal("8.00"))
    assert await connection_share.share_for(c, "acc2") == Decimal("8.00")


@pytest.mark.parametrize(("ready", "row"), [(True, None), (False, None)], ids=["not-in-copy", "before-crm-079"])
async def test_the_setting_is_the_fallback(setting: dict[str, Any], ready: bool, row: Any) -> None:
    setting["acc2"] = 5
    c = ScriptedConn().on(READY, ready).on(ONE, row)
    assert await connection_share.share_for(c, "acc2") == Decimal("5")
    assert (c.sql(ONE) == []) is (not ready)


@pytest.mark.parametrize(("spent", "expected"), [("7", (None, None)), ("8", ("connection_budget_exhausted", "budget"))])
async def test_the_gateway_enforces_the_copy(setting: dict[str, Any], spent: str, expected: tuple[Any, Any]) -> None:
    setting["acc2"] = 5
    c = ScriptedConn().on(READY, True).on(ONE, Decimal("8.00")).on(SPEND, Decimal(spent))
    assert await connection_cap.refusal(c, BedrockLike()) == expected


async def test_a_zero_share_stops_the_connection_at_once(setting: dict[str, Any]) -> None:
    c = ScriptedConn().on(READY, True).on(ONE, Decimal("0.00")).on(SPEND, Decimal("0"))
    assert await connection_cap.refusal(c, BedrockLike()) == ("connection_budget_exhausted", "budget")


@pytest.mark.parametrize(
    "items",
    [
        [{"connection_key": "zz_bad", "share_usd": "1"}],
        [{"connection_key": "zz-a", "share_usd": "1"}, {"connection_key": "zz-a", "share_usd": "2"}],
        [{"connection_key": "zz-a", "share_usd": "-1"}],
        [{"connection_key": "zz-a", "share_usd": "1000001"}],
        [{"connection_key": "zz-a", "share_usd": "lots"}],
    ],
)
async def test_check_copy_refuses_bad_items(items: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError):
        connection_share.check_copy(items)


async def test_write_copy_replaces_the_whole_copy() -> None:
    c = ScriptedConn().on(READY, True).on("FROM crm.llm_connection_share ORDER BY", [])
    shares = connection_share.check_copy(
        [{"connection_key": "zz-a", "share_usd": "8"}, {"connection_key": "zz-b", "share_usd": "0"}]
    )
    await connection_share.write_copy(c, shares, reason=REASON, actor="platform:zz")
    assert c.sql("DELETE FROM crm.llm_connection_share") == [(["zz-a", "zz-b"],)]
    assert [a[:2] for a in c.sql("INSERT INTO crm.llm_connection_share")] == [
        ("zz-a", Decimal("8.00")),
        ("zz-b", Decimal("0.00")),
    ]
    with pytest.raises(ValueError):
        await connection_share.write_copy(c, shares, reason="too short", actor="platform:zz")


async def test_before_crm_079_the_copy_is_unavailable() -> None:
    c = ScriptedConn().on(READY, False)
    with pytest.raises(connection_share.CopyUnavailable):
        await connection_share.read_copy(c)
    out_c = ScriptedConn().on(READY, True).on("FROM crm.llm_connection_share ORDER BY", [])
    assert await connection_share.read_copy(out_c) == {"shares": [], "updated_at": None}
