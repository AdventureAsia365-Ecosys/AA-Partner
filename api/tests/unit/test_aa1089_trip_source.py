"""AA-1089 (Odoo study rank 48): the AA tour a trip came from, on the trip header (app.catalog.trip_source)."""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.catalog import trip_source
from app.routers import pipeline as pipeline_routes
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx

TRIP = uuid.UUID("00000000-0000-4000-8000-0000000010a9")
TOUR_REF = "6b0f7a1c-2d3e-4f50-8a61-9c7d5e3b1a20"
ORIGIN = "https://go.example.test"


def _row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "origin": "web_booking",
        "tour_source_id": TOUR_REF,
        "departure_id": "dep-lotus-0412",
        "departure_date": date(2027, 4, 12),
        "title": "Lotus Valley Walk",
        "served_state": "served",
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_a_trip_from_book_now_shows_its_tour_departure_and_page() -> None:
    conn = ScriptedConn().on("crm.public_booking_attempt", _row())
    out = await trip_source.trip_view(tenant_ctx(conn), TRIP, ORIGIN + "/")
    assert out == {
        "origin": "web_booking",
        "tour_source_id": TOUR_REF,
        "title": "Lotus Valley Walk",
        "served_state": "served",
        "departure_id": "dep-lotus-0412",
        "departure_date": "2027-04-12",
        "page_url": f"{ORIGIN}/trip/{TOUR_REF}",
    }
    assert conn.sql("crm.public_booking_attempt") == [(TRIP,)]


@pytest.mark.asyncio
async def test_a_trip_without_an_aa_tour_has_no_block() -> None:
    assert await trip_source.trip_view(tenant_ctx(ScriptedConn()), TRIP, ORIGIN) is None


@pytest.mark.asyncio
async def test_an_aa_line_without_a_cached_title_or_date_still_names_the_tour() -> None:
    conn = ScriptedConn().on("crm.booking_line", _row(origin="aa_line", title=None, served_state=None,
                                                      departure_date=None))  # fmt: skip
    out = await trip_source.trip_view(tenant_ctx(conn), TRIP, ORIGIN)
    assert out is not None and out["origin"] == "aa_line" and out["title"] is None
    assert out["departure_date"] is None and out["page_url"] is None  # not known to be served: no link


def test_the_page_link_only_for_a_served_uuid_key_and_a_known_origin() -> None:
    assert trip_source.page_url(ORIGIN, TOUR_REF.upper(), "served") == f"{ORIGIN}/trip/{TOUR_REF}"
    assert trip_source.page_url(ORIGIN, TOUR_REF, "retired") is None
    assert trip_source.page_url(ORIGIN, TOUR_REF, "paused") is None
    assert trip_source.page_url(ORIGIN, "lotus-valley-walk", "served") is None  # CIS 404s a key that is no UUID
    assert trip_source.page_url(None, TOUR_REF, "served") is None
    assert trip_source.page_url("", TOUR_REF, "served") is None


def test_the_query_reads_book_now_first_then_live_aa_lines() -> None:
    sql = " ".join(trip_source.SOURCE_SQL.split())
    assert "a.status = 'created'" in sql and "l.supplier_kind = 'aa'" in sql and "l.status <> 'cancelled'" in sql
    assert "crm.aa_catalog_tour c ON c.source_system = 'cis'" in sql
    assert sql.index("'web_booking'") < sql.index("'aa_line'") and "ORDER BY x.rank" in sql


def test_the_router_takes_the_client_origin_from_the_intake_page_origins() -> None:
    def request(origins: tuple[str, ...] | None) -> SimpleNamespace:
        settings = None if origins is None else SimpleNamespace(intake_page_origins=origins)
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))

    assert pipeline_routes._client_origin(request((ORIGIN, "https://other.example.test"))) == ORIGIN  # type: ignore[arg-type]
    assert pipeline_routes._client_origin(request(())) is None  # type: ignore[arg-type]
    assert pipeline_routes._client_origin(request(None)) is None  # type: ignore[arg-type]
