"""AA-1188: agencies see the right price. The tariff reader takes the 1.7 / 1.12 fields (child, infant and single
prices, availability, departure_adjusted, the group size, early booking days, the coverage counts) and an older answer
without them; the CRM's JSON keeps every B2B price under a ``b2b_*`` name (the margin serializer strips them). The
terms reader and ``GET /crm/v1/aa/terms`` (every role, no-store, Booking refusals and outages mapped). Invented tours
and clauses only."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.booking import aa_terms, contract
from app.booking import tariff as aa_tariff
from app.booking.client import BookingRefusal, BookingUnavailable
from app.errors import Conflict, Invalid, UnavailableRetry
from app.main import create_app
from app.margin import strip_margin
from app.permissions import Action
from app.routers import aa as router
from tests.aa_fakes import FakeBooking, booking_client, tariff_body, terms_body
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx


def _money(amount: str, currency: str = "USD") -> dict[str, str]:
    return {"amount": amount, "currency": currency}


def _body_112() -> dict[str, Any]:
    body = tariff_body("USD")
    first, second = body["items"]
    first.update(
        child_price_per_person=_money("271.60"),
        infant_price=_money("0.00"),
        single_supplement_per_person=_money("95.00"),
        availability="available",
        departure_adjusted=True,
    )
    second.update(single_supplement_per_person=None, availability="on_request", departure_adjusted=False)
    body.update(group_on_request_min_pax=9, early_booking_days=60, tours_priced=1, catalog_tours=844)
    return body


# ── the tariff reader (1.7 / 1.12) ─────────────────────────────────────────────────────────────────


def test_the_reader_takes_the_new_fields() -> None:
    t = contract.tariff(_body_112(), "USD")
    first, second = t.items
    assert (first.child_price_per_person, first.infant_price, first.single_supplement_per_person) == (
        Decimal("271.60"),
        Decimal("0.00"),
        Decimal("95.00"),
    )
    assert (first.availability, first.departure_adjusted) == ("available", True)
    assert (second.availability, second.single_supplement_per_person) == ("on_request", None)
    assert (t.group_on_request_min_pax, t.early_booking_days, t.tours_priced, t.catalog_tours) == (9, 60, 1, 844)


def test_an_older_answer_reads_as_before() -> None:
    t = contract.tariff(tariff_body("USD"), "USD")
    assert all(i.availability == "available" and i.departure_adjusted is False for i in t.items)
    assert all(i.child_price_per_person is None for i in t.items)
    assert (t.group_on_request_min_pax, t.early_booking_days, t.tours_priced, t.catalog_tours) == (None,) * 4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("availability", "sold_out"),
        ("availability", 1),
        ("departure_adjusted", "yes"),
        ("single_supplement_per_person", {"amount": "95.00", "currency": "EUR"}),
        ("child_price_per_person", {"amount": "-1.00", "currency": "USD"}),
        ("infant_price", {"amount": "x", "currency": "USD"}),
    ],
)
def test_a_new_row_field_that_breaks_the_contract_fails_the_read(field: str, value: Any) -> None:
    body = _body_112()
    body["items"][0][field] = value
    with pytest.raises(contract.ContractError):
        contract.tariff(body, "USD")


@pytest.mark.parametrize(
    ("field", "value"),
    [("group_on_request_min_pax", 0), ("tours_priced", -1), ("catalog_tours", "844"), ("early_booking_days", True)],
)
def test_a_new_count_that_breaks_the_contract_fails_the_read(field: str, value: Any) -> None:
    body = _body_112()
    body[field] = value
    with pytest.raises(contract.ContractError):
        contract.tariff(body, "USD")


def test_the_view_names_every_b2b_price_b2b_and_the_serializer_strips_them() -> None:
    view = aa_tariff.view(contract.tariff(_body_112(), "USD"))
    first = view["items"][0]
    assert first["b2b_child_price_per_person"] == _money("271.60")
    assert first["b2b_infant_price"] == _money("0.00")
    assert first["b2b_single_supplement_per_person"] == _money("95.00")
    assert (first["availability"], first["departure_adjusted"]) == ("available", True)
    assert view["items"][1]["b2b_single_supplement_per_person"] is None
    assert (view["tours_priced"], view["catalog_tours"], view["group_on_request_min_pax"]) == (1, 844, 9)
    stripped = strip_margin(view)
    for key in ("b2b_child_price_per_person", "b2b_infant_price", "b2b_single_supplement_per_person"):
        assert key not in stripped["items"][0], key
    assert stripped["items"][0]["availability"] == "available" and stripped["tours_priced"] == 1


# ── the terms reader ─────────────────────────────────────────────────────────────────────────────


def test_the_terms_reader_keeps_what_the_crm_shows() -> None:
    out = contract.partner_terms(terms_body())
    assert out["payment"] == {
        "deposit_pct": "30.00",
        "balance_days_before_departure": 45,
        "settlement_due_days": 14,
        "scope": "default",
    }
    assert [t["min_days"] for t in out["cancellation_tiers"]] == [60, 30, 0]  # the largest first
    assert out["clauses"] == [
        {
            "code": "price_error",
            "title": "Invented price correction",
            "body": "AA corrects an invented price.",
            "free_cancel_days": 3,
            "version": 2,
        }
    ]
    assert out["quote_rules"]["age_bands"] == {"child_age_max": 11, "infant_age_max": 1}
    assert out["holds_paused"] is False and "credit" not in out and "overdue_amount" not in str(out)


def test_terms_without_payment_policy_or_quote_rules() -> None:
    out = contract.partner_terms(terms_body(payment=None, cancellation_policy=None, quote_rules=None))
    assert (out["payment"], out["cancellation_tiers"], out["quote_rules"]) == (None, None, None)
    older = terms_body()
    del older["quote_rules"]  # a 1.11 Booking
    assert contract.partner_terms(older)["quote_rules"] is None


@pytest.mark.parametrize(
    "over",
    [
        {"payment": {"deposit_pct": "thirty", "balance_days_before_departure": 1, "settlement_due_days": 1}},
        {"payment": {"deposit_pct": "30", "balance_days_before_departure": -1, "settlement_due_days": 1}},
        {"payment": {"deposit_pct": "30", "balance_days_before_departure": 1, "settlement_due_days": 1, "scope": "x"}},
        {"cancellation_policy": {"tiers": []}},
        {"cancellation_policy": {"tiers": [{"min_days": 1, "fee_pct": "1e2"}]}},
        {"clauses": [{"code": "Bad Code", "title": "t", "body": "b", "version": 1}]},
        {"clauses": [{"code": "c", "title": "", "body": "b", "version": 1}]},
        {"clauses": "none"},
        {"credit": {"holds_paused": "no"}},
        {"quote_rules": {"quote_validity_days": 14, "group_on_request_min_pax": 9,
                         "age_bands": {"child_age_max": 1, "infant_age_max": 1}}},
        {"as_of": "05/01/2031"},
    ],
)  # fmt: skip
def test_terms_that_break_the_contract_are_refused(over: dict[str, Any]) -> None:
    with pytest.raises(contract.ContractError):
        contract.partner_terms(terms_body(**over))


@pytest.mark.parametrize(
    ("exc", "kind", "status", "code"),
    [
        (BookingRefusal(403, "unknown_partner"), Conflict, 409, "aa_partner_not_active"),
        (BookingRefusal(403, "forbidden"), Conflict, 409, "aa_partner_not_active"),
        (BookingRefusal(409, "other_thing"), Invalid, 422, "aa_terms_refused"),
        (BookingUnavailable("booking_timeout"), UnavailableRetry, 503, "booking_unavailable"),
        (contract.ContractError("x"), UnavailableRetry, 503, "booking_unavailable"),
    ],
)
def test_terms_errors_become_clear_problems(exc: Exception, kind: type, status: int, code: str) -> None:
    problem = aa_terms.booking_error(exc)
    assert isinstance(problem, kind) and problem.status_code == status and problem.code == code


# ── GET /crm/v1/aa/terms ────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, booking: Any
) -> tuple[TestClient, Any, list[Any]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), booking_client=booking)
    ctx = tenant_ctx(ScriptedConn(), role=role)
    seen: list[Any] = []

    def tx(*a: Any, **kw: Any) -> Any:
        seen.append(kw.get("action"))
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app, headers=headers), ctx, seen


@pytest.mark.parametrize("role", ["owner", "agent", "assistant"])
def test_every_role_reads_the_terms_no_store(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    fake = FakeBooking()
    client, ctx, seen = _client(pools, monkeypatch, role, booking_client(fake))
    r = client.get("/crm/v1/aa/terms")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["quote_rules"]["group_on_request_min_pax"] == 9
    assert seen == [Action.TRIP_VIEW] and fake.calls["GET terms"] == 1
    assert fake.tenants[-1] == str(TENANT)


@pytest.mark.parametrize(("refusal", "status", "code"), [("forbidden", 409, "aa_partner_not_active"),
                                                        ("other_thing", 422, "aa_terms_refused")])  # fmt: skip
def test_booking_refusals_on_the_terms_route(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, refusal: str, status: int, code: str
) -> None:
    fake = FakeBooking(tariff_refusal=refusal)
    client, _, _ = _client(pools, monkeypatch, "owner", booking_client(fake))
    r = client.get("/crm/v1/aa/terms")
    assert (r.status_code, r.json()["detail"]) == (status, code)
    assert "booking-side detail" not in r.text


def test_booking_down_or_not_connected(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(pools, monkeypatch, "owner", booking_client(FakeBooking(down=True)))
    r = client.get("/crm/v1/aa/terms")
    assert r.status_code == 503 and r.json()["detail"] == "booking_unavailable" and r.headers["retry-after"]
    client, _, seen = _client(pools, monkeypatch, "owner", None)
    r = client.get("/crm/v1/aa/terms")
    assert r.status_code == 503 and r.json()["detail"] == "booking_not_configured" and seen == []
