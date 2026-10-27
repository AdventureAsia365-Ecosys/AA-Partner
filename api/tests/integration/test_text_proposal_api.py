"""AA-951 (card CRM-22) end to end on PostgreSQL with the RDS role shape: the text proposal, the Before you go day 0
and the message templates as crm_app through ``tenant_tx()``, with real field encryption and an in-memory Booking.

- **Card R**: a text rendered before the version changed carries another ``price_hash``; its send is 409
  ``price_hash_mismatch`` and nothing is written (a text draft whose amounts changed; a hash that is not the
  version's).
- **Card E**: a trip without AA items gets no pack suggestion, refuses a pack pick (422 ``no_aa_items``) and renders
  day 0 without any pack (the insurance reminder only).
- Day 0 with an AA item in Nepal: the Nepal pack is suggested, picked, copied into the published version; its visa
  section renders the official links only; a trekking tour adds the fitness warning.
- E-mail and WhatsApp, EN and VI, from one snapshot with one hash; the WhatsApp send sends the version (link made)
  and answers the text with the link; an e-mail without a mailbox is 409 and the version stays unsent.
- R9: the AA starter templates are copied once per group and locale; a template's merge fields fill the intro.
- F4 / R38: the client page C-03 opened from the link in the text shows the same Before you go (the packs with the
  visa section reduced to its links, the reminders) and a priced day 0 item is in every text too.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Invalid
from app.mail import templates
from app.proposals import builder, prep_packs, service, text_proposal
from app.proposals import public as page
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking
from tests.integration.test_builder_api import _aa, _as, _block, _create, _option, _pid, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NEPAL_EN = (
    "## Entry and visa\n\nMost travellers need a visa, which many passports can get on arrival. Check the rules on "
    "the official website: [immigration.gov.np](https://www.immigration.gov.np).\n\n## Money\n\nThe currency is the "
    "Nepalese rupee (NPR)."
)
NEPAL_VI = "## Nhập cảnh và thị thực\n\nXem [immigration.gov.np](https://www.immigration.gov.np).\n\n## Tiền\n\nNPR."
WELCOME = (
    "Hi {{traveller_first_name}},\n\nHere is {{trip_name}} with {{agency_name}}.\n\n{{proposal}}\n\n{{agent_name}}"
)


async def _content(pool: asyncpg.Pool) -> None:
    rows = [
        ("prep_pack", "en", "prep-pack-nepal", "Before you go: Nepal", NEPAL_EN, {"country_codes": ["NP"]}),
        ("prep_pack", "vi", "prep-pack-nepal", "Trước khi đi: Nepal", NEPAL_VI, {"country_codes": ["NP"]}),
        ("prep_pack", "en", "prep-pack-japan", "Before you go: Japan", "## Money\n\nYen.", {"country_codes": ["JP"]}),
        ("starter_template", "en", "template-welcome-message", "Welcome", WELCOME, {}),
        ("starter_template", "vi", "template-welcome-message", "Chào mừng", "Chào {{traveller_first_name}}", {}),
    ]
    async with pool.acquire() as conn:
        for kind, locale, group, title, body, applies in rows:
            await conn.execute(
                "INSERT INTO crm.aa_content (content_id, kind, locale, group_key, version, title, body_markdown, "
                "applies_to, content_hash, published_at) VALUES ($1, $2, $3, $4, 2, $5, $6, $7::jsonb, $8, now()) "
                "ON CONFLICT DO NOTHING",
                uuid.uuid5(uuid.NAMESPACE_URL, f"{group}:{locale}"), kind, locale, group, title, body,
                json.dumps(applies), f"sha256:{group}:{locale}",
            )  # fmt: skip
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state, body) "
            "VALUES ('T-951', 'Annapurna trek 5 days', 'NP', 5, 'served', $1::jsonb) "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served', body = EXCLUDED.body",
            json.dumps({"facets": {"intensity": "challenging", "activities": ["trekking"]}}),
        )


async def _published(pool: asyncpg.Pool, s: dict[str, Any], *, aa: bool) -> dict[str, Any]:
    v = await _create(pool, s)
    block = _block(v, 1)
    if aa:
        v = await _aa(pool, s, FakeBooking(b2b_price="900.00"), v, tour_source_id="T-951", sell_amount=Decimal("1200"))
        v = await _option(pool, s, v, block, title="Guesthouse", sell_amount=Decimal("1100"))
    else:
        v = await _option(pool, s, v, block, title="Homestay", sell_amount=Decimal("200"))
        v = await _option(pool, s, v, block, title="Riverside Lodge", sell_amount=Decimal("240"))
    v = await _as(pool, s, "agent", builder.update_settings, _pid(v), v["edit_seq"],
                  {"balance_due_on": s["start"] - dt.timedelta(days=30)})  # fmt: skip
    return v


async def _publish(pool: asyncpg.Pool, s: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
    return await _as(pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)


async def test_a_trip_without_aa_items_renders_without_packs(app_pool: asyncpg.Pool) -> None:
    await _content(app_pool)
    s = await _setup(app_pool)
    v = await _published(app_pool, s, aa=False)
    assert v["before_you_go"]["aa_items"] == 0 and v["before_you_go"]["suggested"] == []
    assert "prep_packs_not_chosen" not in v["checks"]["warnings"]
    with pytest.raises(Invalid) as err:  # card E: no AA item, no pack
        await _as(app_pool, s, "agent", builder.set_before_you_go, _pid(v), v["edit_seq"], ["prep-pack-nepal"])
    assert err.value.code == "no_aa_items"
    pub = await _publish(app_pool, s, v)
    text = await _as(app_pool, s, "agent", text_proposal.preview, _pid(pub), fmt="email", locale="en")
    assert text["price_hash"] == pub["price_hash"] and text["reference"] in text["body"]
    assert "Before you go" in text["body"] and "Travel insurance" in text["body"]
    assert "immigration.gov.np" not in text["body"] and "Fitness" not in text["body"]
    # D1: the alternative shows its difference only.
    assert "Riverside Lodge (+40.00 USD)" in text["body"] and "Total: 200.00 USD" in text["body"]


async def test_packs_from_the_aa_items_visa_link_only_and_one_hash_for_every_text(app_pool: asyncpg.Pool) -> None:
    await _content(app_pool)
    s = await _setup(app_pool)
    v = await _published(app_pool, s, aa=True)
    byg = v["before_you_go"]
    assert byg["aa_items"] == 1 and byg["countries"] == ["NP"] and byg["fitness_warning"] is True
    assert [p["group_key"] for p in byg["suggested"]] == ["prep-pack-nepal"]  # not Japan
    assert "prep_packs_not_chosen" in v["checks"]["warnings"]
    with pytest.raises(Invalid) as err:
        await _as(app_pool, s, "agent", builder.set_before_you_go, _pid(v), v["edit_seq"], ["prep-pack-bhutan"])
    assert err.value.code == "pack_not_found"
    v = await _as(app_pool, s, "agent", builder.set_before_you_go, _pid(v), v["edit_seq"], ["prep-pack-nepal"])
    assert v["before_you_go"]["selected"][0]["locales"] == ["en", "vi"]
    pub = await _publish(app_pool, s, v)
    texts = {
        (fmt, loc): await _as(app_pool, s, "assistant", text_proposal.preview, _pid(pub), fmt=fmt, locale=loc)
        for fmt in ("email", "whatsapp") for loc in ("en", "vi")
    }  # fmt: skip
    assert {t["price_hash"] for t in texts.values()} == {pub["price_hash"]}
    en = texts[("email", "en")]["body"]
    assert "Before you go: Nepal" in en and "Official information: https://www.immigration.gov.np" in en
    assert "Most travellers need a visa" not in en and "Nepalese rupee" in en and "Fitness:" in en
    assert "900" not in en  # never the net
    vi = texts[("whatsapp", "vi")]["body"]
    assert "*Trước khi đi*" in vi and "Thông tin chính thức: https://www.immigration.gov.np" in vi
    assert texts[("email", "vi")]["subject"].startswith("Đề xuất chuyến đi")
    # A newer CIS version does not change the published version's pack (CRM-B5 card E).
    async with app_pool.acquire() as conn:
        await conn.execute("UPDATE crm.aa_content SET body_markdown = 'Changed', version = 3 "
                           "WHERE group_key = 'prep-pack-nepal'")  # fmt: skip
    again = await _as(app_pool, s, "agent", text_proposal.preview, _pid(pub), fmt="email", locale="en")
    assert again["body"] == en
    # A working copy keeps the pinned packs.
    copy = await _create(app_pool, s, from_proposal_id=_pid(pub))
    assert [p["group_key"] for p in copy["before_you_go"]["selected"]] == ["prep-pack-nepal"]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        rows = await prep_packs.selected(ctx, _pid(copy))
    assert {r["content_version"] for r in rows} == {2}


async def test_the_client_page_and_the_text_show_one_before_you_go(app_pool: asyncpg.Pool) -> None:
    await _content(app_pool)
    async with app_pool.acquire() as conn:  # an earlier test publishes a newer CIS version of the pack: restore it
        await conn.execute("UPDATE crm.aa_content SET body_markdown = $1 WHERE group_key = 'prep-pack-nepal' "
                           "AND locale = 'en'", NEPAL_EN)  # fmt: skip
    s = await _setup(app_pool)
    v = await _published(app_pool, s, aa=True)
    v = await _as(app_pool, s, "agent", builder.set_before_you_go, _pid(v), v["edit_seq"], ["prep-pack-nepal"])
    day0 = next(d for d in v["days"] if d["day_no"] == 0)
    v = await _as(app_pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day0["proposal_day_id"]),
                  {"title": "Arrival", "type": "transport"})  # fmt: skip
    v = await _option(app_pool, s, v, _block(v, 0), title="Airport pickup", sell_amount=Decimal("40"))
    pub = await _publish(app_pool, s, v)
    out = await _as(app_pool, s, "agent", text_proposal.send, _pid(pub), fmt="whatsapp", locale="en",
                    price_hash=pub["price_hash"], client_origin="https://go.example.test")  # fmt: skip
    before, _, _days = out["body"].partition("*Day 1")
    assert "- Arrival: Airport pickup" in before and "Official information: https://www.immigration.gov.np" in before
    client = await page.read(app_pool, s["keys"], out["link_path"].rsplit("/", 1)[1])
    byg = client["before_you_go"]
    assert byg["insurance_reminder"] and byg["fitness_warning"] and [p["group_key"] for p in byg["packs"]] == [
        "prep-pack-nepal"]  # fmt: skip
    en = next(x for x in byg["packs"][0]["versions"] if x["locale"] == "en")
    visa = next(sec for sec in en["sections"] if sec["visa"])
    assert visa["text"] == "" and [link["url"] for link in visa["links"]] == ["https://www.immigration.gov.np"]
    assert "Most travellers need a visa" not in json.dumps(byg) and "aa_items" not in byg
    # The day 0 item the text lists is on the client page and in its total.
    day0_page = next(d for d in client["days"] if d["day_no"] == 0)
    assert [o["title"] for o in day0_page["blocks"][0]["options"]] == ["Airport pickup"]


async def test_a_differing_price_hash_blocks_the_send(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    draft = await _as(app_pool, s, "agent", service.create_draft, s["trip"], body="Five days in Nepal.",
                      currency="USD", total=Decimal("1000"), deposit=Decimal("300"),
                      balance_due_on=s["start"], terms_doc_id=None)  # fmt: skip
    pid = uuid.UUID(draft["proposal_id"])
    seen = await _as(app_pool, s, "agent", text_proposal.preview, pid, fmt="whatsapp", locale="en")
    # The agency edits the amounts after the agent rendered the text: the old text's hash is stale.
    await _as(app_pool, s, "agent", service.update_draft, pid, {"total_amount": Decimal("1100")})
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", text_proposal.send, pid, fmt="whatsapp", locale="en",
                  price_hash=seen["price_hash"])  # fmt: skip
    assert err.value.code == "price_hash_mismatch" and err.value.extra["current_price_hash"] != seen["price_hash"]
    overview = await _as(app_pool, s, "agent", service.trip_overview, s["trip"])
    assert overview["proposals"][0]["status"] == "draft"  # nothing written
    # The fresh text sends: the version goes out, the text carries the link.
    fresh = await _as(app_pool, s, "agent", text_proposal.preview, pid, fmt="whatsapp", locale="en")
    out = await _as(app_pool, s, "agent", text_proposal.send, pid, fmt="whatsapp", locale="en",
                    price_hash=fresh["price_hash"], client_origin="https://go.example.test")  # fmt: skip
    assert out["version_sent"] and out["status"] == "sent" and out["link_path"].startswith("/c/p/")
    assert f"https://go.example.test{out['link_path']}" in out["body"] and out["price_hash"] == fresh["price_hash"]
    overview = await _as(app_pool, s, "agent", service.trip_overview, s["trip"])
    assert overview["proposals"][0]["price_hash"] == fresh["price_hash"]
    # Sending the text again: no new link (the client has it), same hash.
    again = await _as(app_pool, s, "agent", text_proposal.send, pid, fmt="whatsapp", locale="vi",
                      price_hash=fresh["price_hash"])  # fmt: skip
    assert not again["version_sent"] and again["link_path"] is None and "Mã tham chiếu" in again["body"]


async def test_an_email_without_a_mailbox_sends_nothing(app_pool: asyncpg.Pool) -> None:
    await _content(app_pool)
    s = await _setup(app_pool)
    pub = await _publish(app_pool, s, await _published(app_pool, s, aa=False))
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", text_proposal.send, _pid(pub), fmt="email", locale="en",
                  price_hash=pub["price_hash"], message_id=uuid.uuid4())  # fmt: skip
    assert err.value.code == "mailbox_not_connected"
    view = await _as(app_pool, s, "agent", builder.view, _pid(pub))
    assert view["status"] == "published"  # the whole send rolled back


async def test_aa_starter_templates_and_merge_fields(app_pool: asyncpg.Pool) -> None:
    await _content(app_pool)
    s = await _setup(app_pool)
    lib = await _as(app_pool, s, "owner", templates.library)
    assert {(t["group_key"], t["locale"]) for t in lib["aa_starters"]} >= {("template-welcome-message", "en")}
    assert (await _as(app_pool, s, "owner", templates.import_starters))["added"] >= 2
    assert (await _as(app_pool, s, "owner", templates.import_starters))["added"] == 0  # converges
    lib = await _as(app_pool, s, "agent", templates.library)
    welcome = next(t for t in lib["templates"] if t["aa_group_key"] == "template-welcome-message" and
                   t["locale"] == "en")  # fmt: skip
    assert welcome["source"] == "aa_starter" and welcome["body"] == WELCOME
    with pytest.raises(Exception) as err:  # the agent does not write the agency's templates
        await _as(app_pool, s, "agent", templates.create, locale="en", title="Mine", body="Hello")
    assert getattr(err.value, "code", "") == "role_forbidden"
    pub = await _publish(app_pool, s, await _published(app_pool, s, aa=False))
    text = await _as(app_pool, s, "agent", text_proposal.preview, _pid(pub), fmt="email", locale="en",
                     template_id=uuid.UUID(welcome["message_template_id"]))  # fmt: skip
    assert text["body"].startswith("Hi Grace,") and "Here is Nepal family" in text["body"]
    assert text["body"].index("Here is") < text["body"].index("Your trip proposal")
    assert text["unfilled"] == ["agent_name"]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert (await text_proposal.current_hash(ctx, Vault(s["keys"], ctx), await ctx.conn.fetchrow(
            text_proposal.ROW_SQL, _pid(pub)))) == pub["price_hash"]  # fmt: skip
