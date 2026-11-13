"""AA-1086 gap G9 on PostgreSQL 15 as crm_app: the Dev-only E2E identities never reach the pilot agency.

On Dev every agency is synthetic (``crm.tenant.is_synthetic``), the pilot included, so the A10c check "synthetic only"
alone let the CRM staff E2E identity into the pilot's real data. The pilot marker (``crm.tenant.is_pilot``, projected
from CIS by ``crm_entitlement_pull``, crm-044) now counts as "not synthetic" for it:

- every staff context refuses the pilot agency to the E2E identity with 403 ``e2e_synthetic_only`` and the lists leave
  it out, while a normal staff member still reaches it;
- the studio invitation relay's fact (:func:`app.tenancy.synthetic_agency`, A10b-1) is false for the pilot;
- the first projection that sees the marker also clears ``is_synthetic`` (one way), so once CIS ends the pilot and
  the feed clears the marker, the former pilot (still the real agency's data) stays out of reach of the E2E
  identities and of the console's test-agency delete.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from app.auth.route_guard import Caller
from app.cis import entitlement_feed as feed
from app.errors import Forbidden
from app.support import reads, sessions
from app.tenancy import synthetic_agency
from tests.integration.test_crm005_intake import _keys, _owner_fetch
from tests.integration.test_crm006_travellers import _inquiry
from tests.integration.test_support_access_api import STAFF_ISS, _grant

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def staff(*groups: str, e2e: bool = False) -> Caller:
    return Caller("staff", "workforce", "crm-staff", STAFF_ISS, str(uuid.uuid4()), groups, e2e_synthetic_only=e2e)


async def _mark(pool: asyncpg.Pool, tenant: uuid.UUID, value: bool) -> None:
    """Set or clear the pilot marker the only way the CRM allows: the CIS account feed's projection."""
    projection = feed.Projection("core", 10, ("ai_assist",), True, True, value)
    assert await feed.apply(pool, tenant, projection, feed.RunCounts()) in ("applied", "unchanged")


async def _flags(admin: asyncpg.Connection, tenant: uuid.UUID) -> tuple[bool, bool]:
    sql = "SELECT is_synthetic, is_pilot FROM crm.tenant WHERE tenant_id = $1"
    [row] = await _owner_fetch(admin, tenant, sql, tenant)
    return bool(row["is_synthetic"]), bool(row["is_pilot"])


async def test_e2e_identities_never_reach_the_pilot_agency(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    pilot_agency, owner, _trip, _lead = await _inquiry(app_pool, keys)
    await _grant(app_pool, owner, pilot_agency)
    e2e = staff("crm-ops", "crm-support", e2e=True)
    assert await synthetic_agency(app_pool, pilot_agency) is True  # an ordinary synthetic agency first

    await _mark(app_pool, pilot_agency, True)
    try:
        assert await _flags(admin, pilot_agency) == (False, True)  # the feed marked it real with the pilot marker
        for attempt in (
            lambda: sessions.start_support(app_pool, e2e, pilot_agency, ticket_ref="SUP-1086"),
            lambda: sessions.my_sessions(app_pool, e2e, pilot_agency),
            lambda: reads.overview(app_pool, e2e, pilot_agency),
        ):
            with pytest.raises(Forbidden) as refused:
                await attempt()
            assert refused.value.code == "e2e_synthetic_only"
        granted = {i["tenant_id"] for i in (await sessions.granted_agencies(app_pool, e2e))["items"]}
        picker = {i["tenant_id"] for i in (await sessions.breakglass_agencies(app_pool, e2e))["items"]}
        assert str(pilot_agency) not in granted | picker
        assert await _owner_fetch(admin, pilot_agency, "SELECT 1 AS ok FROM crm.support_session") == []
        # The A10b-1 studio invitation relay reads the same rule.
        assert await synthetic_agency(app_pool, pilot_agency) is False
        # A normal staff member is unchanged: the pilot's owner granted support.
        support = staff("crm-support")
        support_granted = {i["tenant_id"] for i in (await sessions.granted_agencies(app_pool, support))["items"]}
        assert str(pilot_agency) in support_granted
        assert (await sessions.my_sessions(app_pool, support, pilot_agency))["sessions"] == []
    finally:
        await _mark(app_pool, pilot_agency, False)

    # The pilot ended (CIS cleared the marker, the feed followed): the agency stays real, so the E2E identities and
    # the console's test-agency delete still leave it alone; a normal staff member is unchanged.
    assert await _flags(admin, pilot_agency) == (False, False)
    assert await synthetic_agency(app_pool, pilot_agency) is False
    with pytest.raises(Forbidden) as refused:
        await sessions.my_sessions(app_pool, e2e, pilot_agency)
    assert refused.value.code == "e2e_synthetic_only"
    picker = {i["tenant_id"] for i in (await sessions.breakglass_agencies(app_pool, e2e))["items"]}
    assert str(pilot_agency) not in picker
    assert (await sessions.my_sessions(app_pool, staff("crm-support"), pilot_agency))["sessions"] == []
    # A re-projection never sets it back to synthetic.
    await _mark(app_pool, pilot_agency, False)
    assert await _flags(admin, pilot_agency) == (False, False)
