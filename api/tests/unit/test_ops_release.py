"""AA-956 (card CRM-26) without a database: route classes and groups, the manifest CSV, the purge date, the job
schedule, the Booking incident admin link and the ``BOOKING_ADMIN_URL`` setting.

- **R**: staff without ``crm-ops`` -> 403 (``crm-support`` and any other WORKFORCE group); a ``crm-staff`` token on
  the agency routes and a partner token on the staff routes are refused by the class guard.
- **E**: dates moved -> purge date recomputed (``purge_on`` follows ``end_date``; the integration test drives it).
- Every staff route is a GET (the staff class stays read-only, AA-971).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.errors import Invalid
from app.jobs.kinds import OPS_RELEASE_SYNC
from app.jobs.payload_guard import check_payload
from app.jobs.worker import PermanentError
from app.main import create_app
from app.ops_release import job, service, staff
from app.settings import SettingsError, load_settings
from tests.tokens import LocalPools

TENANT = "00000000-0000-4000-8000-000000000001"
TRIP = "00000000-0000-4000-8000-0000000000aa"
STAFF_PATHS = (
    "/crm/staff/v1/ops-releases",
    f"/crm/staff/v1/agencies/{TENANT}/trips/{TRIP}/ops-release",
    f"/crm/staff/v1/agencies/{TENANT}/trips/{TRIP}/ops-release/manifest",
)


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


# ── routes ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", STAFF_PATHS)
@pytest.mark.parametrize("groups", [["crm-support"], ["cis-admin"], []])
def test_staff_without_crm_ops_is_403(client: TestClient, pools: LocalPools, path: str, groups: list[str]) -> None:
    res = client.get(path, headers=_bearer(pools.crm_staff(groups)))
    assert res.status_code == 403, (path, groups)


@pytest.mark.parametrize("path", STAFF_PATHS)
def test_crm_ops_passes_the_guards(client: TestClient, pools: LocalPools, path: str) -> None:
    """Past the class and group guards the route needs the database (none configured here): 503, not 401 / 403."""
    res = client.get(path, headers=_bearer(pools.crm_staff(["crm-ops"])))
    assert (res.status_code, res.json()["detail"]) == (503, "database_not_configured")


@pytest.mark.parametrize("path", STAFF_PATHS)
def test_a_partner_token_is_refused_on_the_staff_routes(client: TestClient, pools: LocalPools, path: str) -> None:
    assert client.get(path, headers=_bearer(pools.partner_web())).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", f"/crm/v1/trips/{TRIP}/ops-release"), ("POST", f"/crm/v1/trips/{TRIP}/ops-release/withdraw")],
)
def test_a_staff_token_is_refused_on_the_agency_routes(
    client: TestClient, pools: LocalPools, method: str, path: str
) -> None:
    res = client.request(method, path, headers={**_bearer(pools.crm_staff(["crm-ops"])), "X-AA-Tenant-Id": TENANT})
    assert (res.status_code, res.json()["detail"]) == (403, "wrong_audience")


def test_every_ops_release_staff_route_is_a_get(client: TestClient) -> None:
    spec = client.app.openapi()
    paths = spec["paths"].items()
    ours = [(m.upper(), p) for p, ops in paths if "ops-release" in p and "/crm/staff/" in p for m in ops]
    assert len(ours) == len(STAFF_PATHS)
    assert {m for m, _ in ours} == {"GET"}


def test_an_unknown_manifest_field_is_422(client: TestClient, pools: LocalPools) -> None:
    res = client.get(STAFF_PATHS[2] + "?fields=phone", headers=_bearer(pools.crm_staff(["crm-ops"])))
    assert res.status_code == 422


# ── the manifest ───────────────────────────────────────────────────────────────────────────────────

DOC: dict[str, Any] = {
    "v": 1,
    "travellers": [
        {
            "name": "Lotus Mai",
            "given_name": "Mai",
            "family_name": "Lotus",
            "age_band": "adult",
            "dietary": ["vegetarian"],
            "mobility": [],
            "arrival": {
                "flight_no": "ZZ101",
                "airport": "HAN",
                "sched_local": "2026-12-01T22:40",
                "tz": "Asia/Bangkok",
            },
            "departure": None,
        },
        {"name": "=HYPERLINK(1)", "age_band": "child", "dietary": [], "mobility": ["wheelchair"], "arrival": None},
    ],
}


def test_manifest_has_the_watermark_on_every_row_and_only_the_picked_fields() -> None:
    text, rows = staff.manifest_csv(DOC, ("name", "dietary", "arrival_flight"), "AAOPS-0123456789")
    lines = text.strip().split("\r\n")
    assert rows == 2
    assert lines[0] == "watermark,no,name,dietary,arrival_flight"
    assert lines[1] == "AAOPS-0123456789,1,Mai Lotus,vegetarian,ZZ101 HAN 2026-12-01T22:40 Asia/Bangkok"
    assert all(line.startswith("AAOPS-0123456789,") for line in lines[1:])
    assert "age_band" not in lines[0] and "insurance" not in text


def test_manifest_cells_never_start_a_formula() -> None:
    text, _ = staff.manifest_csv(DOC, ("name",), "AAOPS-0123456789")
    assert ",'=HYPERLINK(1)" in text


def test_manifest_fields_default_and_unknown() -> None:
    assert staff.pick_fields([]) == staff.MANIFEST_DEFAULT
    assert staff.pick_fields(["departure_flight", "name"]) == ("name", "departure_flight")
    with pytest.raises(Invalid):
        staff.pick_fields(["phone"])


def test_watermark_fits_the_crm034_check() -> None:
    import re

    marks = {staff.watermark() for _ in range(20)}
    assert len(marks) == 20
    assert all(re.fullmatch(r"[A-Z0-9-]{8,40}", m) for m in marks)


# ── purge date, incident link, settings ──────────────────────────────────────────────────────────────


def test_purge_date_follows_the_return_date() -> None:
    assert service.purge_on(dt.date(2026, 12, 10)) == dt.date(2027, 3, 10)
    assert service.purge_on(dt.date(2026, 12, 20)) == dt.date(2027, 3, 20)  # dates moved -> purge moved
    assert service.purge_on(None) is None


def test_incident_admin_link_is_the_booking_b2b_desk() -> None:
    assert staff.incident_admin_url("https://booking.example.invalid") == "https://booking.example.invalid/b2b-desk"
    assert staff.incident_admin_url("") is None


def test_booking_admin_url_setting() -> None:
    assert load_settings({}).booking_admin_url == ""
    got = load_settings({"BOOKING_ADMIN_URL": "https://Booking.example.invalid/"})
    assert got.booking_admin_url == "https://booking.example.invalid"
    for bad in ("http://booking.example.invalid", "https://booking.example.invalid/b2b-desk"):
        with pytest.raises(SettingsError, match="BOOKING_ADMIN_URL"):
            load_settings({"BOOKING_ADMIN_URL": bad})


# ── the job ────────────────────────────────────────────────────────────────────────────────────────


def test_schedule_is_one_row_per_utc_hour() -> None:
    at = dt.datetime(2026, 12, 1, 7, 59, tzinfo=dt.UTC)
    assert job.schedule_key(at) == f"{OPS_RELEASE_SYNC}:schedule:2026-12-01T07"
    assert job.schedule_key(at + dt.timedelta(minutes=1)) == f"{OPS_RELEASE_SYNC}:schedule:2026-12-01T08"


def test_nudge_payload_is_ids_only() -> None:
    check_payload({"tenant_id": TENANT, "trip_id": TRIP})
    assert job._ids({"tenant_id": TENANT, "trip_id": TRIP}) == (uuid.UUID(TENANT), uuid.UUID(TRIP))
    assert job._ids({"purpose": "hourly_schedule"}) is None
    with pytest.raises(PermanentError):
        job._ids({"tenant_id": "x", "trip_id": TRIP})
