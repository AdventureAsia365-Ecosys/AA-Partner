"""AA-968 (card CRM-36) without a database: the d21 content gate, the spread over days, the unsubscribe lines and
headers, the settings the campaigns read, the job and route registrations. Invented names and places only."""

from __future__ import annotations

import datetime as dt
import email
import email.policy
from typing import Any

import pytest

from app import platform_settings as ps
from app.campaigns import audience, model
from app.jobs import kinds
from app.jobs.handlers import SCHEDULED_KINDS, build_hooks
from app.mail import compose

TOUR = "3e7a91c4-58b2-4f0d-a6e1-9c2b7d4f8a16"
OTHER = "9d1c2b3a-4e5f-4a6b-8c7d-0e1f2a3b4c5d"
CLIENT = "https://go.crm.example.test"
PLANNER = "https://plan.example.test"
DAY = dt.date(2027, 3, 1)


def _gate(body: str, **over: Any) -> list[str]:
    args: dict[str, Any] = {
        "subject": "Spring on the river",
        "body": body,
        "aa_tour_ids": [TOUR],
        "tour_states": {TOUR: "served"},
        "content_listed": None,
        "brand_only": False,
        "client_origin": CLIENT,
        "tripplanner_origin": PLANNER,
    }
    args.update(over)
    return model.content_gate(**args)


@pytest.mark.parametrize(
    ("body", "codes"),
    [
        (f"See {CLIENT}/trip/{TOUR}.", []),
        (f"See {CLIENT}/trip/{TOUR.upper()}/ now", []),
        (f"Plan it: {PLANNER}/sen-vang/plan, then write to us at mailto:hello@agency.example.test", []),
        ("Call us on tel:+84900000000", []),
        (f"See {CLIENT}/trip/{OTHER}", ["tour_link_not_listed"]),
        (f"See {CLIENT}/pricing", ["link_not_allowed"]),
        (f"See http://go.crm.example.test/trip/{TOUR}", ["link_not_allowed"]),
        ("Deals at https://other-agency.example.test/deal", ["link_not_allowed"]),
        ("Deals at www.other-agency.example.test", ["link_not_allowed"]),
        (f"Sneaky https://user@go.crm.example.test/trip/{TOUR}", ["link_not_allowed"]),
        (f"Port https://go.crm.example.test:8443/trip/{TOUR}", ["link_not_allowed"]),
    ],
)
def test_links_go_only_to_aa_tour_pages_or_the_planner(body: str, codes: list[str]) -> None:
    assert _gate(body) == codes


def test_the_aa_subject_rules() -> None:
    assert _gate("Hello", aa_tour_ids=[]) == ["no_aa_subject"]
    assert _gate("Hello", aa_tour_ids=[], content_listed=True) == []
    assert _gate("Hello", aa_tour_ids=[], content_listed=False) == ["aa_content_unlisted"]
    assert _gate("Hello", tour_states={TOUR: "retired"}) == ["aa_tour_not_served"]
    assert _gate("Hello", tour_states={}) == ["aa_tour_not_served"]  # not in the cache
    assert _gate("Thank you for travelling with us.", aa_tour_ids=[], brand_only=True) == []
    assert _gate(f"Plan: {PLANNER}/x", aa_tour_ids=[], brand_only=True) == ["brand_has_sales_link"]
    assert _gate("", subject=" ") == ["empty_body", "empty_subject"]


def test_without_a_planner_origin_planner_links_are_refused() -> None:
    assert _gate(f"Plan: {PLANNER}/x", tripplanner_origin=None) == ["link_not_allowed"]
    assert _gate(f"See {CLIENT}/trip/{TOUR}", client_origin=None) == ["link_not_allowed"]


def test_hold_codes() -> None:
    assert model.hold_for([]) is None
    assert model.hold_for(["aa_tour_not_served", "link_not_allowed"]) == "aa_tour_retired"
    assert model.hold_for(["link_not_allowed"]) == "content_gate"


@pytest.mark.parametrize(
    ("count", "limit", "used", "want"),
    [
        (0, 3, {}, []),
        (3, 3, {}, [0, 0, 0]),
        (5, 2, {}, [0, 0, 1, 1, 2]),
        (3, 2, {0: 1}, [0, 1, 1]),
        (2, 2, {0: 5, 1: 2}, [2, 2]),
    ],
)
def test_a_list_over_the_daily_limit_is_spread_over_days(
    count: int, limit: int, used: dict[int, int], want: list[int]
) -> None:
    taken = {DAY + dt.timedelta(days=k): v for k, v in used.items()}
    assert model.spread(count, DAY, limit, taken) == [DAY + dt.timedelta(days=k) for k in want]


def test_spread_needs_a_positive_limit() -> None:
    with pytest.raises(ValueError):
        model.spread(1, DAY, 0, {})


def test_unsubscribe_lines_and_links() -> None:
    assert model.page_url(CLIENT + "/", "tok") == f"{CLIENT}/c/u/tok"
    assert model.one_click_url(CLIENT, "tok") == f"{CLIENT}/crm/public/v1/unsubscribe/tok"
    assert model.footer("vi", "Sen Vang", "L").endswith("Huỷ nhận tin: L")
    assert "Unsubscribe: L" in model.footer("fr", "Sen Vang", "L")  # unknown locale -> English


def test_a_campaign_mail_carries_the_one_click_headers() -> None:
    built = compose.build(
        from_address="hello@agency.example.test",
        from_name="Sen Vang",
        to_address="guest@example.test",
        subject="Spring",
        text="Hello",
        mail_token=None,
        list_unsubscribe=f"{CLIENT}/crm/public/v1/unsubscribe/tok",
    )
    msg = email.message_from_bytes(built.raw, policy=email.policy.default)
    assert msg["List-Unsubscribe"] == f"<{CLIENT}/crm/public/v1/unsubscribe/tok>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    plain = compose.build(from_address="a@b.example.test", from_name=None, to_address="c@d.example.test",
                          subject="x", text="y", mail_token=None)  # fmt: skip
    assert "List-Unsubscribe" not in email.message_from_bytes(plain.raw, policy=email.policy.default)


def test_the_limits_are_settings() -> None:
    limit = ps.READER.definition(ps.CAMPAIGN_MAILBOX_DAILY_LIMIT)
    cap = ps.READER.definition(ps.CAMPAIGN_MAX_RECIPIENTS)
    assert limit.tenant_scoped and cap.tenant_scoped  # a platform value with an agency's own value (the range)
    assert ps.get(ps.CAMPAIGN_MAILBOX_DAILY_LIMIT) == 400 and ps.get(ps.CAMPAIGN_MAX_RECIPIENTS) == 2000


@pytest.mark.asyncio
async def test_the_daily_limit_is_read_per_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    async def tenant_get(_conn: Any, key: str, tenant_id: str) -> int:
        seen.append((key, tenant_id))
        return 7

    class _Ctx:
        conn = object()
        tenant_id = "tenant-a"

    monkeypatch.setattr(ps, "tenant_get", tenant_get)
    assert await audience.daily_limit(_Ctx()) == 7  # type: ignore[arg-type]
    assert await audience.max_recipients(_Ctx()) == 7  # type: ignore[arg-type]
    assert seen == [(ps.CAMPAIGN_MAILBOX_DAILY_LIMIT, "tenant-a"), (ps.CAMPAIGN_MAX_RECIPIENTS, "tenant-a")]


def test_the_send_pass_is_an_hourly_due_now_kind() -> None:
    assert kinds.CAMPAIGN_TICK in kinds.EXPECTED_SECONDS and kinds.CAMPAIGN_TICK in SCHEDULED_KINDS
    assert kinds.CAMPAIGN_TICK not in kinds.DEPLOY_GATE_EXEMPT
    assert "HourlyCampaignTick" in {type(h).__name__ for h in build_hooks(retention_utc_hour=19)}


def test_links_from_the_settings() -> None:
    class _Settings:
        intake_page_origins = (CLIENT + "/", "https://other.example.test")
        tripplanner_site_origin = ""

    links = audience.links_from(_Settings())
    assert (links.client_origin, links.tripplanner_origin) == (CLIENT, None)
    assert audience.links_from(None) == audience.Links(None, None)


def test_exclusion_order() -> None:
    row = {"has_email": True, "email_bidx": b"x" * 16, "marketing": True, "suppressed": False}
    assert audience.exclusion(row) is None
    assert audience.exclusion({**row, "has_email": False}) == "no_email"
    assert audience.exclusion({**row, "marketing": None}) == "no_consent"
    assert audience.exclusion({**row, "marketing": False, "suppressed": True}) == "no_consent"
    assert audience.exclusion({**row, "suppressed": True}) == "suppressed"
