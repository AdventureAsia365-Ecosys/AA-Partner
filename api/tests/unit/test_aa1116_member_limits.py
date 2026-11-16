"""AA-1116 (option B, crm-075): the CRM member limit of an agency with no seats from CIS and the ceiling of the
projected ``crm.seats`` are admin-editable settings (``crm.members.default_limit`` 3, ``crm.members.max_limit`` 500),
read at the time of use: an approved platform value takes effect with no deploy. Invented ids only.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import platform_settings as ps
from app.cis import entitlement_feed as feed
from app.members import team
from app.platform_settings.reader import _Row
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx
from tests.unit.test_entitlement_feed import A, account


@pytest.fixture(autouse=True)
def _clean() -> Any:
    ps.invalidate()
    ps.READER._snapshot = {}
    yield
    ps.invalidate()
    ps.READER._snapshot = None


def approved(key: str, value: Any) -> None:
    ps.READER._snapshot[(key, None)] = _Row(value=value, version=2)  # type: ignore[index]


def _seats(seats: str) -> feed.Projection:
    return feed.projection(feed.parse_account(account(ents={"crm.seats": seats}), A))


def test_defaults_are_the_seeded_values() -> None:
    assert (team.default_member_limit(), team.max_member_limit()) == (3, 500)
    assert team.default_plan() == {"plan": "free", "member_limit": 3, "allowed_features": []}
    assert ps.definition(ps.MEMBERS_DEFAULT_LIMIT).approval == "maker_checker"
    assert ps.definition(ps.MEMBERS_MAX_LIMIT).scope_kind == "platform"


def test_an_approved_value_is_read_at_the_time_of_use() -> None:
    approved(ps.MEMBERS_DEFAULT_LIMIT, 5)
    approved(ps.MEMBERS_MAX_LIMIT, 40)
    assert team.default_member_limit() == 5 and _seats("600.00").member_limit == 40
    approved(ps.MEMBERS_DEFAULT_LIMIT, 80)  # never above the ceiling
    assert team.default_member_limit() == 40
    assert _seats("12.00").member_limit == 12  # the seats CIS sells win under the ceiling


@pytest.mark.asyncio
async def test_plan_without_a_row_uses_the_setting() -> None:
    approved(ps.MEMBERS_DEFAULT_LIMIT, 4)
    conn = ScriptedConn().on("SELECT (SELECT count(*) FROM crm.member", 1)
    assert (await team.plan(tenant_ctx(conn)))["member_limit"] == 4
