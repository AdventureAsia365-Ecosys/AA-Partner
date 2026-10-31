"""AA-971 (card CRM-13b) route rules, without a database.

R: support export / send -> 403. The staff class has no export, send, role, integration or settings route at all (every
``/crm/staff/v1`` route is a GET except starting and ending one's own session), and a ``crm-staff`` token on the
agency class (export, send) is refused before any handler. Groups: a support session needs ``crm-support``, break-glass
``crm-ops``. The break-glass reason list is one list in three places (route, mail, crm-033 CHECK).
"""

from __future__ import annotations

import datetime as dt
import inspect
import re
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import tenancy
from app.main import create_app
from app.members.mail import BREAKGLASS_REASONS, breakglass_message
from app.routers.staff import BreakglassReason
from app.support import sessions
from tests.tokens import LocalPools

TENANT = "00000000-0000-4000-8000-000000000001"
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "033_crm_support_access.sql"
SESSION_WRITES = {
    ("POST", "/crm/staff/v1/support/sessions"),
    ("POST", "/crm/staff/v1/support/sessions/end"),
    ("POST", "/crm/staff/v1/breakglass/sessions"),
    ("POST", "/crm/staff/v1/breakglass/sessions/end"),
}


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def client(pools: LocalPools) -> Iterator[TestClient]:
    settings = pools.settings()
    with TestClient(create_app(settings, verifier=pools.verifier(settings))) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_staff_class_is_read_only_but_for_ones_own_session(client: TestClient) -> None:
    spec = client.app.openapi()
    staff = [(m.upper(), p) for p, ops in spec["paths"].items() if p.startswith("/crm/staff/v1/") for m in ops]
    assert SESSION_WRITES <= set(staff)
    assert [r for r in staff if r[0] != "GET" and r not in SESSION_WRITES] == []
    words = re.compile(r"export|send|message|invite|role|integration|settings|mailbox|download", re.I)
    assert [p for _m, p in staff if words.search(p)] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/crm/v1/data/exports"),
        ("POST", f"/crm/v1/proposals/{TENANT}/send"),
        ("POST", f"/crm/v1/trips/{TENANT}/stage2/send"),
        ("GET", "/crm/v1/support-access"),
        ("POST", "/crm/v1/support-access/revoke"),
    ],
)
def test_a_staff_token_is_refused_on_the_agency_class(
    client: TestClient, pools: LocalPools, method: str, path: str
) -> None:
    for group in ("crm-support", "crm-ops"):
        res = client.request(method, path, headers={**_bearer(pools.crm_staff([group])), "X-AA-Tenant-Id": TENANT})
        assert (res.status_code, res.json()["detail"]) == (403, "wrong_audience"), (path, group)


def test_session_starts_need_their_group(client: TestClient, pools: LocalPools) -> None:
    res = client.post(
        "/crm/staff/v1/support/sessions",
        json={"tenant_id": TENANT, "ticket_ref": "SUP-1"},
        headers=_bearer(pools.crm_staff(["crm-ops"])),
    )
    assert (res.status_code, res.json()["detail"]) == (403, "missing_group")
    res = client.post(
        "/crm/staff/v1/breakglass/sessions",
        json={"tenant_id": TENANT, "ticket_ref": "INC-1", "reason_code": "service_outage"},
        headers=_bearer(pools.crm_staff(["crm-support"])),
    )
    assert (res.status_code, res.json()["detail"]) == (403, "missing_group")
    res = client.get("/crm/staff/v1/support/agencies", headers=_bearer(pools.crm_staff(["crm-ops"])))
    assert (res.status_code, res.json()["detail"]) == (403, "missing_group")


@pytest.mark.parametrize(
    "body",
    [
        {"tenant_id": TENANT, "ticket_ref": "INC-1", "reason_code": "curiosity"},
        {"tenant_id": TENANT, "ticket_ref": "free text", "reason_code": "service_outage"},
        {"tenant_id": TENANT, "ticket_ref": "INC-1", "reason_code": "service_outage", "minutes": 61},
        {"tenant_id": TENANT, "ticket_ref": "INC-1", "reason_code": "service_outage", "minutes": 5},
        {"tenant_id": TENANT, "ticket_ref": "INC-1", "reason_code": "service_outage", "note": "x"},
    ],
)
def test_break_glass_takes_a_listed_reason_a_ticket_and_at_most_an_hour(
    client: TestClient, pools: LocalPools, body: dict
) -> None:
    res = client.post("/crm/staff/v1/breakglass/sessions", json=body, headers=_bearer(pools.crm_staff(["crm-ops"])))
    assert res.status_code == 422


def test_a_partner_or_cis_admin_token_is_refused_on_the_staff_class(client: TestClient, pools: LocalPools) -> None:
    for token in (pools.partner_web(), pools.cis_admin(), pools.breakglass()):
        res = client.get(f"/crm/staff/v1/agencies/{TENANT}/overview", headers=_bearer(token))
        assert (res.status_code, res.json()["detail"]) == (403, "wrong_audience")


def test_one_reason_list_in_route_mail_and_migration() -> None:
    route = set(typing.get_args(BreakglassReason))
    assert route == set(BREAKGLASS_REASONS)
    check = re.search(r"reason_code IN \(([^)]*)\)", MIGRATION.read_text(encoding="utf-8"))
    assert check is not None
    assert set(re.findall(r"'([a-z_]+)'", check.group(1))) == route


def test_the_owner_mail_names_no_staff_member_or_ticket() -> None:
    at = dt.datetime(2026, 11, 2, 9, 30, tzinfo=dt.UTC)
    msg = breakglass_message(
        "owner@example.test",
        agency="Lotus Trails",
        reason_code="service_outage",
        opened_at=at,
        expires_at=at + dt.timedelta(minutes=30),
        link="https://partner.example.test",
    )
    assert msg.kind == "breakglass_notice" and "Lotus Trails" in msg.subject
    assert "a service outage" in msg.text and "/settings/access-log" in msg.text and "02 Nov 2026 10:00 UTC" in msg.text
    assert "INC-" not in msg.text and "aa-" not in msg.text
    assert "client names are not shown" in msg.text


def test_break_glass_never_shows_identity() -> None:
    """Review S262: in-app break-glass has no MFA step-up and skips the owner's consent, so it opens without F1
    names: the session row says so (crm-033 CHECK), the start SQL writes false and the read context ignores the flag
    for any kind but support. Lifting it needs the AWS-level break-glass role with MFA (a later migration)."""
    text = MIGRATION.read_text(encoding="utf-8")
    check = re.search(r"kind = 'breakglass' AND support_grant_id IS NULL AND (NOT )?show_identity", text)
    assert check is not None and check.group(1) == "NOT "
    assert "VALUES ('breakglass', $1, $2, $3, $4, false," in sessions.START_BREAKGLASS_SQL
    assert 'show_identity=bool(row["show_identity"]) and kind == "support"' in inspect.getsource(tenancy.support_tx)
