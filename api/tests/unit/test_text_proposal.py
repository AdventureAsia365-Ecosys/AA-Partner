"""AA-951 (card CRM-22) against scripted connections: the day 0 packs (:mod:`app.proposals.prep_packs`), the text
proposal preview and send (:mod:`app.proposals.text_proposal`; card R: a differing ``price_hash`` blocks the send;
card E: no AA item, no pack), the template library (:mod:`app.mail.templates`) and the AA content sync
(:mod:`app.catalog.content` with the client of :mod:`app.catalog.client`). The SQL, the guards and the end-to-end flows
run on PostgreSQL in tests/integration/test_text_proposal_api.py and test_crm022_schema.py."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

import pytest

from app.catalog import content as content_sync
from app.catalog import store
from app.catalog.client import CatalogUnavailable, ContentClient, content, content_summary
from app.catalog.sync import CatalogSync
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs.queue import Job
from app.mail import templates
from app.proposals import prep_packs, service, text_proposal
from tests.aa_fakes import FakeCis, catalog_config, content_body
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, tenant_ctx

P, TRIP = uuid.uuid4(), uuid.uuid4()
D0, D1 = uuid.uuid4(), uuid.uuid4()
B1, O1, O2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
HASH = bytes(range(32))
NEPAL = {"country_codes": ["NP"]}


def _pack(group: str, locale: str = "en", applies: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    row = {
        "content_id": str(uuid.uuid4()), "kind": "prep_pack", "locale": locale, "group_key": group, "version": 2,
        "title": f"Before you go: {group} ({locale})", "body_markdown": "## Visa\n\nSee https://evisa.example.gov.",
        "applies_to": json.dumps(applies if applies is not None else NEPAL), "content_hash": "sha256:x",
        "published_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
    }  # fmt: skip
    row.update(over)
    return row


def _selected(group: str, locale: str = "en", **over: Any) -> dict[str, Any]:
    row = {
        "prep_pack_selection_id": uuid.uuid4(), "group_key": group, "locale": locale, "content_id": uuid.uuid4(),
        "content_version": 2, "title": f"Pack {group} {locale}", "body_markdown": "## Visa\n\nhttps://evisa.example.gov",
        "applies_to": NEPAL, "content_hash": "sha256:x", "sort_order": 0,
    }  # fmt: skip
    row.update(over)
    return row


def _conn(*, aa: int = 1, tours: list[dict[str, Any]] | None = None, picks: list[dict[str, Any]] | None = None,
          packs: list[dict[str, Any]] | None = None) -> ScriptedConn:  # fmt: skip
    conn = ScriptedConn()
    conn.on("FROM crm.aa_catalog_tour c", tours if tours is not None else [
        {"source_id": "T-1", "title": "Annapurna trek", "country_code": "NP", "facets": None}])  # fmt: skip
    conn.on("SELECT (SELECT count(*) FROM crm.option ao", aa)
    conn.on("FROM crm.prep_pack_selection WHERE proposal_id", picks or [])
    conn.on("kind = 'prep_pack' AND listed AND group_key IS NOT NULL", packs if packs is not None else [
        _pack("prep-pack-nepal"), _pack("prep-pack-nepal", "vi"), _pack("prep-pack-japan", applies={
            "country_codes": ["JP"]})])  # fmt: skip
    conn.on("SELECT default_locale FROM crm.tenant", "vi-VN")
    return conn


# ── prep packs: pure helpers ─────────────────────────────────────────────────────────────────────


def test_sections_keep_only_the_official_links_of_a_visa_section() -> None:
    body = (
        "## Nhập cảnh và thị thực\n\nNhiều hộ chiếu được miễn. [evisa.gov.vn](https://evisa.gov.vn). "
        "Also https://evisa.gov.vn and https://immigration.example.gov.\n\n## Money\n\nPay by **Visa card**: "
        "[bank](https://bank.example)\n\n- [ ] Passport"
    )
    secs = prep_packs.sections(body)
    assert (
        secs[0].visa
        and secs[0].text == ""
        and secs[0].official_links
        == (
            ("evisa.gov.vn", "https://evisa.gov.vn"),
            ("https://immigration.example.gov", "https://immigration.example.gov"),
        )
    )
    assert not secs[1].visa and secs[1].text == "Pay by Visa card: bank (https://bank.example)\n\n- Passport"
    # Without headings: paragraphs; decomposed Vietnamese is normalised first.
    plain = prep_packs.sections("Thi\u0323 thu\u031b\u0323c: https://a.example.gov\n\nBring a hat.")
    assert [s.visa for s in plain] == [True, False] and plain[1].heading is None
    assert prep_packs.sections("# Only a heading") == [prep_packs.Section("Only a heading", "")]
    assert prep_packs.plain("[https://x.example](https://x.example)") == "https://x.example"


def test_relevance_fitness_and_locales() -> None:
    packs = [_pack("a"), _pack("b", applies={"tours": [{"source_system": "cis", "source_id": "T-9"}]}),
             _pack("c", applies={}), _pack("a", "vi")]  # fmt: skip
    assert prep_packs.relevant(packs, {"NP"}, set()) == ["a"]
    assert prep_packs.relevant(packs, set(), {"T-9"}) == ["b"]
    assert prep_packs.relevant(packs, set(), set()) == []  # an applies-everywhere pack is not about this trip
    assert prep_packs.fitness_warning([{"title": "City walk", "facets": json.dumps({"intensity": "strenuous"})}])
    assert prep_packs.fitness_warning([{"title": "Poon Hill trek", "facets": None}])
    assert prep_packs.fitness_warning([{"title": "X", "facets": {"activities": ["high_altitude"]}}])
    assert not prep_packs.fitness_warning([{"title": "Hoi An food walk", "facets": {"intensity": "easy"}}])
    rows = [{"locale": "vi", "x": 1}, {"locale": "en", "x": 2}]
    assert prep_packs.best(rows, "vi")["x"] == 1 and prep_packs.best(rows, "fr")["x"] == 2
    assert prep_packs.best([{"locale": "vi"}], "en")["locale"] == "vi"
    assert prep_packs.ui_locale("vi-VN") == "vi" and prep_packs.ui_locale(None) == "en"


# ── prep packs: with a context ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_day_0_view_suggests_packs_from_the_aa_items() -> None:
    conn = _conn(picks=[_selected("prep-pack-nepal"), _selected("prep-pack-nepal", "vi")])
    out = await prep_packs.view(tenant_ctx(conn), P, TRIP)
    assert out["countries"] == ["NP"] and out["fitness_warning"] and out["insurance_reminder"]
    assert [s["group_key"] for s in out["suggested"]] == ["prep-pack-nepal"]
    assert out["suggested"][0]["title"].endswith("(vi)") and out["suggested"][0]["selected"]
    assert out["selected"][0]["locales"] == ["en", "vi"] and out["selected"][0]["suggested"]
    assert out["packs_not_chosen"] is False


@pytest.mark.asyncio
async def test_card_e_no_aa_item_no_suggestion_and_no_pick() -> None:
    conn = _conn(aa=0, tours=[])
    out = await prep_packs.view(tenant_ctx(conn), P, TRIP, "en")
    assert out["suggested"] == [] and out["packs_not_chosen"] is False and out["aa_items"] == 0
    with pytest.raises(Invalid) as err:
        await prep_packs.replace(tenant_ctx(conn), P, TRIP, ["prep-pack-nepal"])
    assert err.value.code == "no_aa_items"
    await prep_packs.replace(tenant_ctx(conn), P, TRIP, [])  # clearing is always allowed
    assert conn.sql("DELETE FROM crm.prep_pack_selection")


@pytest.mark.asyncio
async def test_replace_copies_every_locale_of_each_pack() -> None:
    conn = _conn()
    conn.on("AND listed AND group_key = ANY", [_pack("prep-pack-nepal"), _pack("prep-pack-nepal", "vi")])
    assert await prep_packs.replace(tenant_ctx(conn), P, TRIP, ["prep-pack-nepal", "prep-pack-nepal"]) == 1
    inserts = conn.sql("INSERT INTO crm.prep_pack_selection")
    assert [(a[2], a[3], a[10]) for a in inserts] == [("prep-pack-nepal", "en", 0), ("prep-pack-nepal", "vi", 0)]
    with pytest.raises(Invalid) as err:
        await prep_packs.replace(tenant_ctx(conn), P, TRIP, ["prep-pack-bhutan"])
    assert err.value.code == "pack_not_found"
    with pytest.raises(Invalid):
        await prep_packs.replace(tenant_ctx(conn), P, TRIP, [f"p-{i:02d}" for i in range(13)])


@pytest.mark.asyncio
async def test_copy_and_the_route_reader() -> None:
    conn = _conn(picks=[_selected("prep-pack-nepal")])
    await prep_packs.copy(tenant_ctx(conn), uuid.uuid4(), P, TRIP)
    assert conn.sql("INSERT INTO crm.prep_pack_selection")[0][5] == 2  # the pinned version, not the cache's
    conn.on("SELECT trip_id, kind FROM crm.proposal", None)
    with pytest.raises(NotFound):
        await prep_packs.for_proposal(tenant_ctx(conn), P)
    conn.on("SELECT trip_id, kind FROM crm.proposal", {"trip_id": TRIP, "kind": "text"})
    with pytest.raises(Conflict):
        await prep_packs.for_proposal(tenant_ctx(conn), P)
    conn.on("SELECT trip_id, kind FROM crm.proposal", {"trip_id": TRIP, "kind": "builder"})
    assert (await prep_packs.for_proposal(tenant_ctx(conn), P))["proposal_id"] == str(P)


# ── text proposal ────────────────────────────────────────────────────────────────────────────────


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "proposal_id": P, "trip_id": TRIP, "version": 2, "status": "published", "kind": "builder",
        "body_enc": b"enc:proposal.body_enc:Intro", "currency": "USD ", "total_amount": Decimal("300"),
        "deposit_amount": Decimal("90"), "balance_due_on": dt.date(2026, 11, 1), "terms_doc_id": None,
        "price_hash": HASH, "price_visibility": "total_with_deltas", "room_config": '[{"type": "double", "adults": 2}]',
        "deposit_pct": Decimal("30"), "starts_on": dt.date(2026, 12, 1), "link_id": None, "trip_title": "Nepal",
        "stage": "proposal", "start_date": dt.date(2026, 12, 1), "destinations": ["nepal", "ha_giang"],
    }  # fmt: skip
    row.update(over)
    return row


def _builder_conn(row: dict[str, Any], **kw: Any) -> ScriptedConn:
    conn = _conn(**kw)
    conn.on("p.link_id, t.title AS trip_title", row)
    conn.on("SELECT display_name, default_locale FROM crm.tenant", {"display_name": "Sen Vang", "default_locale": "en"})
    conn.on("FROM crm.proposal_day", [
        {"proposal_day_id": D0, "day_no": 0, "title": None, "place": None, "tz": "Asia/Kathmandu",
         "notes_enc": b"enc:proposal_day.notes_enc:Warm layers", "source_day_id": None},
        {"proposal_day_id": D1, "day_no": 1, "title": "Arrive", "place": "Kathmandu", "tz": "Asia/Kathmandu",
         "notes_enc": None, "source_day_id": None},
    ])  # fmt: skip
    conn.on("FROM crm.option_block WHERE proposal_id", [
        {"option_block_id": B1, "proposal_day_id": D1, "sort_order": 0, "title": "Lodging", "type": "lodging",
         "client_may_swap": True, "default_option_id": O1, "default_pinned": False, "source_block_id": None},
    ])  # fmt: skip
    opt = {"option_block_id": B1, "sort_order": 0, "source": "agency", "tier": None, "agency_supplier_id": None,
           "net_amount": Decimal("1"), "pay_on_arrival": False, "choose_later_allowed": False,
           "availability": "agency", "availability_checked_at": None, "aa_tour_source_id": None,
           "aa_component_id": None, "aa_component_kind": None, "aa_departure_id": None, "aa_nights": None,
           "aa_quote_id": None, "aa_quote_valid_until": None, "aa_from_price": None, "below_from_price": None,
           "source_option_id": None, "aa_served_state": None}  # fmt: skip
    conn.on("FROM crm.option o", [
        {**opt, "option_id": O1, "title": "Homestay", "sell_amount": Decimal("300")},
        {**opt, "option_id": O2, "title": "Lodge", "sell_amount": Decimal("340"), "availability": "on_request"},
        {**opt, "option_id": uuid.uuid4(), "title": "Gone", "sell_amount": Decimal("1"), "availability": "sold_out"},
    ])  # fmt: skip
    conn.on("FROM crm.option_rule WHERE proposal_id", [])
    return conn


@pytest.mark.asyncio
async def test_preview_renders_the_published_version_with_its_hash() -> None:
    conn = _builder_conn(_row(status="sent"), picks=[_selected("prep-pack-nepal")])
    out = await text_proposal.preview(tenant_ctx(conn, role="assistant"), FakeVault(), P, fmt="email", locale=None)
    assert out["price_hash"] == HASH.hex() and out["locale"] == "vi" and out["link"] == "client_has_it"
    body = out["body"]
    assert "Lựa chọn khác: Lodge (+40,00 USD, theo yêu cầu)" in body and "Gone" not in body
    assert "Warm layers" in body and "Pack prep-pack-nepal en" in body and "https://evisa.example.gov" in body
    assert "Thể lực" in body  # "Annapurna trek"


@pytest.mark.asyncio
async def test_preview_without_aa_items_renders_no_pack() -> None:
    conn = _builder_conn(_row(), aa=0, tours=[], picks=[_selected("prep-pack-nepal")])
    out = await text_proposal.preview(tenant_ctx(conn), FakeVault(), P, fmt="whatsapp", locale="en")
    assert "Pack prep-pack-nepal" not in out["body"] and "*Before you go*" in out["body"]


@pytest.mark.asyncio
async def test_which_versions_render() -> None:
    for over, code in (
        ({"status": "draft"}, "proposal_not_published"),
        ({"status": "superseded"}, "proposal_not_sendable"),
        ({"kind": "text", "status": "withdrawn"}, "proposal_not_sendable"),
    ):
        with pytest.raises(Conflict) as err:
            await text_proposal.preview(tenant_ctx(_builder_conn(_row(**over))), FakeVault(), P, fmt="email",
                                        locale="en")  # fmt: skip
        assert err.value.code == code
    conn = _builder_conn(_row())
    conn.on("p.link_id, t.title AS trip_title", None)
    with pytest.raises(NotFound):
        await text_proposal.preview(tenant_ctx(conn), FakeVault(), P, fmt="email", locale="en")
    with pytest.raises(Invalid):
        await text_proposal.preview(tenant_ctx(conn), FakeVault(), P, fmt="sms", locale="en")
    with pytest.raises(Invalid):
        await text_proposal.preview(tenant_ctx(conn), FakeVault(), P, fmt="email", locale="fr")


@pytest.mark.asyncio
async def test_a_text_draft_hashes_as_send_would_fix_it() -> None:
    row = _row(kind="text", status="draft", price_hash=None, terms_doc_id=uuid.uuid4())
    conn = _builder_conn(row)
    conn.on("SELECT body_sha256 FROM crm.agreement_doc", b"t" * 32)
    want = service.price_hash(proposal_id=P, version=2, currency="USD ", total=Decimal("300"), deposit=Decimal("90"),
                              balance_due_on=dt.date(2026, 11, 1), body="Intro", terms_doc_id=row["terms_doc_id"],
                              terms_sha256=b"t" * 32).hex()  # fmt: skip
    out = await text_proposal.preview(tenant_ctx(conn), FakeVault(), P, fmt="email", locale="en")
    assert out["price_hash"] == want and out["link"] == "added_on_send" and "Intro" in out["body"]


@pytest.mark.asyncio
async def test_a_text_draft_without_terms_hashes_with_the_published_terms_send_pins() -> None:
    """S261 live finding (AA-952 x AA-955): Send pins the agency's published booking terms to a version without
    terms (:func:`service.pin_current_terms`), so the preview's hash is computed with them too."""
    current = {"doc_id": uuid.uuid4(), "version_label": "v2026-10", "body_text": "Terms", "body_sha256": b"c" * 32,
               "published_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC)}  # fmt: skip
    conn = _builder_conn(_row(kind="text", status="draft", price_hash=None, terms_doc_id=None))
    conn.on("kind = 'booking_terms' AND status = 'published'", current)
    want = service.price_hash(proposal_id=P, version=2, currency="USD ", total=Decimal("300"), deposit=Decimal("90"),
                              balance_due_on=dt.date(2026, 11, 1), body="Intro", terms_doc_id=current["doc_id"],
                              terms_sha256=b"c" * 32).hex()  # fmt: skip
    out = await text_proposal.preview(tenant_ctx(conn), FakeVault(), P, fmt="email", locale="en")
    assert out["price_hash"] == want


@pytest.mark.asyncio
async def test_pin_current_terms() -> None:
    current = {"doc_id": uuid.uuid4(), "version_label": "v2026-10", "body_text": "Terms", "body_sha256": b"c" * 32,
               "published_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC)}  # fmt: skip
    conn = ScriptedConn().on("kind = 'booking_terms' AND status = 'published'", current)
    ctx = tenant_ctx(conn)
    row = await service.pin_current_terms(ctx, {"proposal_id": P, "terms_doc_id": None})
    assert row["terms_doc_id"] == current["doc_id"]
    assert conn.sql("SET terms_doc_id = $2 WHERE proposal_id = $1 AND terms_doc_id IS NULL") == [(P, current["doc_id"])]
    assert ctx.audit_events[-1].action == "proposal_terms_pin" and ctx.audit_events[-1].reason == "current_terms"
    named = {"proposal_id": P, "terms_doc_id": uuid.uuid4()}
    conn = ScriptedConn().on("kind = 'booking_terms' AND status = 'published'", current)
    assert await service.pin_current_terms(tenant_ctx(conn), named) is named and not conn.calls
    conn = ScriptedConn()  # the agency has no published terms
    blank = {"proposal_id": P, "terms_doc_id": None}
    assert await service.pin_current_terms(tenant_ctx(conn), blank) is blank and not conn.sql("SET terms_doc_id")


@pytest.mark.asyncio
async def test_card_r_a_differing_price_hash_blocks_the_send(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def no_send(*_a: Any, **_k: Any) -> Any:
        called.append("send")

    monkeypatch.setattr(text_proposal.service, "send", no_send)
    monkeypatch.setattr(text_proposal.conversations, "send", no_send)
    conn = _builder_conn(_row())
    with pytest.raises(Conflict) as err:
        await text_proposal.send(tenant_ctx(conn), FakeVault(), P, fmt="whatsapp", locale="en", price_hash="0" * 64)
    assert err.value.code == "price_hash_mismatch" and err.value.extra["current_price_hash"] == HASH.hex()
    assert called == [] and not conn.sql("UPDATE crm.") and not conn.sql("INSERT")


@pytest.mark.asyncio
async def test_send_sends_the_version_and_puts_the_link_in_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def send_version(_ctx: Any, _vault: Any, pid: uuid.UUID) -> dict[str, Any]:
        return {"price_hash": HASH.hex(), "link_path": "/c/p/tok", "superseded": None}

    mails: list[dict[str, Any]] = []

    async def send_mail(_ctx: Any, _vault: Any, message_id: Any, **kw: Any) -> dict[str, Any]:
        mails.append({"message_id": message_id, **kw})
        return {"message_id": str(message_id), "status": "queued"}

    monkeypatch.setattr(text_proposal.service, "send", send_version)
    monkeypatch.setattr(text_proposal.conversations, "send", send_mail)
    ctx = tenant_ctx(_builder_conn(_row(status="sent")), role="agent")
    out = await text_proposal.send(ctx, FakeVault(), P, fmt="whatsapp", locale="en", price_hash=HASH.hex().upper(),
                                   client_origin="https://go.example.test/")  # fmt: skip
    assert out["link_path"] is None and not out["version_sent"] and not out["link_in_text"]  # the client has it
    # A published version goes out (the link send) and its link is in the text.
    conn = _builder_conn(_row())
    ctx = tenant_ctx(conn, role="agent")
    message = uuid.uuid4()
    with pytest.raises(Invalid):
        await text_proposal.send(ctx, FakeVault(), P, fmt="email", locale="en", price_hash=HASH.hex())
    out = await text_proposal.send(ctx, FakeVault(), P, fmt="email", locale="en", price_hash=HASH.hex(),
                                   message_id=message, client_origin="https://go.example.test",
                                   intro="Dear guest,\n\n{{proposal}}")  # fmt: skip
    assert out["version_sent"] and out["link_in_text"] and "https://go.example.test/c/p/tok" in out["body"]
    assert out["body"].startswith("Dear guest,") and mails[0]["subject"] == out["subject"]
    assert mails[0]["trip_id"] == TRIP and mails[0]["body"] == out["body"]
    assert ctx.audit_events[-1].action == "proposal_text_send" and ctx.audit_events[-1].reason == "email"
    with pytest.raises(Invalid):
        await text_proposal.send(ctx, FakeVault(), P, fmt="whatsapp", locale="en", price_hash=HASH.hex(),
                                 intro="x" * 20_001)  # fmt: skip


@pytest.mark.asyncio
async def test_the_assistant_sends_e_mail_on_assigned_trips_only(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _builder_conn(_row())
    conn.on("SELECT owner_member_id FROM crm.trip WHERE trip_id", uuid.uuid4())
    with pytest.raises(Forbidden):
        await text_proposal.send(tenant_ctx(conn, role="assistant"), FakeVault(), P, fmt="email", locale="en",
                                 price_hash=HASH.hex(), message_id=uuid.uuid4())  # fmt: skip


@pytest.mark.asyncio
async def test_merge_fields_read_the_display_name_never_the_given_name() -> None:
    conn = _builder_conn(_row(status="sent"))
    template_id = uuid.uuid4()
    conn.on("FROM crm.message_template WHERE message_template_id", {
        "message_template_id": template_id, "locale": "en", "title": "Hi", "source": "agency", "aa_group_key": None,
        "body_enc": b"enc:message_template.body_enc:Hi {{traveller_first_name}}, {{destination}} {{departure_date}}",
    })  # fmt: skip
    conn.on(
        "FROM crm.trip_traveler tt JOIN crm.traveler t",
        {
            "traveler_id": uuid.uuid4(),
            # given_name is an F1 click-to-reveal field (reveal with a reason): a preview never opens it.
            "given_name_enc": b"enc:traveler.given_name_enc:Secret",
            "display_name_enc": b"enc:traveler.display_name_enc:Lan Ho",
        },
    )
    vault = FakeVault()
    ctx = tenant_ctx(conn)
    out = await text_proposal.preview(ctx, vault, P, fmt="email", locale="en", template_id=template_id)
    assert out["body"].startswith("Hi Lan, Nepal, Ha Giang 1 Dec 2026") and ("traveler", "display_name_enc") in (
        vault.decrypted)  # fmt: skip
    assert "Secret" not in out["body"] and ("traveler", "given_name_enc") not in vault.decrypted
    assert "given_name" not in ctx.audit_events[-1].fields and "display_name" in ctx.audit_events[-1].fields
    assert "given_name_enc" not in text_proposal.LEAD_NAME_SQL
    conn.on("FROM crm.trip_traveler tt JOIN crm.traveler t", None)
    out = await text_proposal.preview(tenant_ctx(conn), vault, P, fmt="email", locale="vi", template_id=template_id)
    assert out["body"].startswith("Hi bạn,")


@pytest.mark.asyncio
async def test_a_priced_block_on_day_0_is_in_every_text() -> None:
    """A block on day 0 (a visa service, an airport transfer) is in the total and on the client page, so the e-mail
    and the WhatsApp text list it under Before you go too (they carry the same price_hash)."""
    conn = _builder_conn(_row(status="sent"))
    b0, v1 = uuid.uuid4(), uuid.uuid4()
    conn.on("FROM crm.option_block WHERE proposal_id", [
        {"option_block_id": b0, "proposal_day_id": D0, "sort_order": 0, "title": "Arrival", "type": "transport",
         "client_may_swap": False, "default_option_id": v1, "default_pinned": False, "source_block_id": None},
        {"option_block_id": B1, "proposal_day_id": D1, "sort_order": 0, "title": "Lodging", "type": "lodging",
         "client_may_swap": True, "default_option_id": O1, "default_pinned": False, "source_block_id": None},
    ])  # fmt: skip
    options = next(answer for fragment, answer in conn.rules if fragment == "FROM crm.option o")
    conn.on("FROM crm.option o", [*options, {**options[0], "option_block_id": b0, "option_id": v1,
                                             "title": "Airport pickup", "sell_amount": Decimal("40")}])  # fmt: skip
    for fmt in ("email", "whatsapp"):
        body = (await text_proposal.preview(tenant_ctx(conn), FakeVault(), P, fmt=fmt, locale="en"))["body"]
        before, _, days = body.partition("Day 1")
        assert "Before you go" in before and "- Arrival: Airport pickup" in before
        assert "Airport pickup" not in days and "- Lodging: Homestay" in days


@pytest.mark.asyncio
async def test_new_link_reissues_the_link_of_a_sent_version_into_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The e-mail that carried the link was undone (the version stayed sent): the next send asks for a new link."""
    reissued: list[uuid.UUID] = []

    async def reissue(_ctx: Any, pid: uuid.UUID) -> dict[str, Any]:
        reissued.append(pid)
        return {"proposal_id": str(pid), "link_path": "/c/p/new"}

    async def send_mail(_ctx: Any, _vault: Any, message_id: Any, **kw: Any) -> dict[str, Any]:
        return {"message_id": str(message_id), "status": "queued"}

    async def no_send(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a sent version is not sent again")

    monkeypatch.setattr(text_proposal.service, "reissue_link", reissue)
    monkeypatch.setattr(text_proposal.service, "send", no_send)
    monkeypatch.setattr(text_proposal.conversations, "send", send_mail)
    ctx = tenant_ctx(_builder_conn(_row(status="sent")), role="agent")
    out = await text_proposal.send(ctx, FakeVault(), P, fmt="email", locale="en", price_hash=HASH.hex(),
                                   message_id=uuid.uuid4(), client_origin="https://go.example.test",
                                   new_link=True)  # fmt: skip
    assert reissued == [P] and out["link_reissued"] and out["link_in_text"] and not out["version_sent"]
    assert out["link_path"] == "/c/p/new" and "https://go.example.test/c/p/new" in out["body"]
    # Without the flag a sent version keeps its link and the text carries none.
    out = await text_proposal.send(ctx, FakeVault(), P, fmt="whatsapp", locale="en", price_hash=HASH.hex())
    assert reissued == [P] and not out["link_reissued"] and out["link_path"] is None


@pytest.mark.asyncio
async def test_the_client_page_gets_the_same_before_you_go() -> None:
    """C-03 (R38, F4): every locale of each picked pack, a visa section reduced to its official links, the reminders;
    nothing without an AA item (card E), the same gate as the texts."""
    picks = [_selected("prep-pack-nepal", body_markdown="## Visa\n\nGet one at https://evisa.example.gov.\n\n"
                                                         "## Pack\n\nWarm layers."),
             _selected("prep-pack-nepal", "vi")]  # fmt: skip
    out = await prep_packs.client_view(tenant_ctx(_conn(picks=picks)), P, TRIP)
    assert out["insurance_reminder"] and out["fitness_warning"] and len(out["packs"]) == 1
    versions = out["packs"][0]["versions"]
    assert [v["locale"] for v in versions] == ["en", "vi"] and out["packs"][0]["group_key"] == "prep-pack-nepal"
    visa, pack = versions[0]["sections"]
    assert visa == {"heading": "Visa", "text": "", "visa": True,
                    "links": [{"label": "https://evisa.example.gov", "url": "https://evisa.example.gov"}]}  # fmt: skip
    assert pack["text"] == "Warm layers." and not pack["visa"]
    none = await prep_packs.client_view(tenant_ctx(_conn(aa=0, tours=[], picks=picks)), P, TRIP)
    assert none == {"packs": [], "insurance_reminder": True, "fitness_warning": False}


# ── templates ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_template_library() -> None:
    tid = uuid.uuid4()
    conn = ScriptedConn()
    conn.on("FROM crm.message_template WHERE deleted_at IS NULL", [{
        "message_template_id": tid, "locale": "en", "title": "Welcome", "source": "aa_starter",
        "aa_group_key": "template-welcome-message", "aa_version": 1, "updated_at": dt.datetime(2026, 10, 1),
        "body_enc": b"enc:message_template.body_enc:Hello"}])  # fmt: skip
    conn.on("SELECT aa_group_key, locale FROM crm.message_template", [{"aa_group_key": "template-welcome-message",
                                                                       "locale": "en"}])  # fmt: skip
    starters = [_pack("template-welcome-message", kind="starter_template", body_markdown="Hi"),
                _pack("template-welcome-message", "vi", kind="starter_template", body_markdown="Chào")]  # fmt: skip
    conn.on("kind = 'starter_template' AND listed", starters)
    out = await templates.library(tenant_ctx(conn, role="assistant"), FakeVault())
    assert out["templates"][0]["body"] == "Hello" and [(s["group_key"], s["locale"]) for s in out["aa_starters"]] == [
        ("template-welcome-message", "vi")]  # fmt: skip
    conn.on("INSERT INTO crm.message_template", lambda *a: a[0] if a[1] == "vi" else None)
    assert (await templates.import_starters(tenant_ctx(conn), FakeVault()))["added"] == 1
    for fn, kw in ((templates.import_starters, {}), (templates.create, {"locale": "en", "title": "x", "body": "y"})):
        with pytest.raises(Forbidden):
            await fn(tenant_ctx(conn, role="agent"), FakeVault(), **kw)
    made = await templates.create(tenant_ctx(conn), FakeVault(), locale="en", title="  New   one ", body="Body")
    assert conn.sql("INSERT INTO crm.message_template")[-1][2] == "New one" and made["message_template_id"]
    for bad in ({"title": " "}, {"title": "x" * 121}, {"body": " "}, {"body": "x" * 20_001}):
        with pytest.raises(Invalid):
            await templates.create(tenant_ctx(conn), FakeVault(), **{"locale": "en", "title": "t", "body": "b", **bad})


@pytest.mark.asyncio
async def test_template_edit_delete_and_get() -> None:
    tid = uuid.uuid4()
    conn = ScriptedConn()
    with pytest.raises(NotFound):
        await templates.update(tenant_ctx(conn), FakeVault(), tid, locale=None, title="x", body=None)
    with pytest.raises(NotFound):
        await templates.delete(tenant_ctx(conn), tid)
    with pytest.raises(NotFound):
        await templates.get(tenant_ctx(conn), FakeVault(), tid)
    conn.on("WHERE message_template_id = $1 AND deleted_at IS NULL", {
        "message_template_id": tid, "locale": "vi", "title": "T", "body_enc": b"enc:message_template.body_enc:B",
        "source": "agency", "aa_group_key": None})  # fmt: skip
    assert (await templates.get(tenant_ctx(conn, role="assistant"), FakeVault(), tid))["body"] == "B"
    await templates.update(tenant_ctx(conn), FakeVault(), tid, locale="en", title=None, body="New")
    args = conn.sql("UPDATE crm.message_template")[0]
    assert args[1] == "en" and args[2] is None and args[3] == b"enc:message_template.body_enc:New"
    await templates.update(tenant_ctx(conn), FakeVault(), tid, locale=None, title="T2", body=None)
    assert (await templates.delete(tenant_ctx(conn), tid))["status"] == "deleted"
    with pytest.raises(Forbidden):
        await templates.delete(tenant_ctx(conn, role="agent"), tid)


# ── AA content: client and sync ──────────────────────────────────────────────────────────────────


def test_the_content_contract_shapes() -> None:
    cid = str(uuid.uuid4())
    body = content_body(cid)
    c = content(body)
    assert c.applies_to == {"country_codes": ["VN"]} and c.group_key == "prep-ha-giang"
    assert content_summary({k: body[k] for k in ("content_id", "kind", "version", "locale", "group_key", "title",
                                                 "content_hash")}).version == 3  # fmt: skip
    assert content(content_body(cid, applies_to={"tours": [{"source_system": "cis", "source_id": "T-1"}]})).applies_to
    for bad in ({"kind": "news"}, {"locale": "fr"}, {"version": 0}, {"group_key": "Bad"}, {"title": "a\nb"},
                {"content_id": "x"}, {"body_markdown": 3}, {"applies_to": {"regions": []}},
                {"applies_to": {"country_codes": ["vn"]}}, {"applies_to": {"tours": [{"source_system": "x"}]}},
                {"applies_to": {"tours": "x"}}, {"published_at": ""}):  # fmt: skip
        with pytest.raises(CatalogUnavailable):
            content({**content_body(cid), **bad})
    with pytest.raises(CatalogUnavailable):
        content_summary([])


@pytest.mark.asyncio
async def test_the_sync_copies_new_versions_and_unlists_retired_items(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [str(uuid.UUID(int=i)) for i in range(1, 5)]
    cis = FakeCis(content={
        ids[0]: content_body(ids[0]),
        ids[1]: content_body(ids[1], locale="vi"),
        ids[2]: content_body(ids[2], kind="starter_template", group_key="template-welcome-message", applies_to={}),
        ids[3]: content_body(ids[3], kind="campaign_block", group_key="campaign-x", applies_to={}),
    })  # fmt: skip
    known = {"prep_pack": {ids[1]: (3, "sha256:9a0364b9e99bb480")}, "starter_template": {}}
    stored: list[str] = []
    unlisted: list[tuple[str, list[str]]] = []

    async def fake_known(_pool: Any, kind: str) -> dict[str, Any]:
        return known[kind]

    async def fake_upsert(_pool: Any, c: Any) -> None:
        stored.append(c.content_id)

    async def fake_touch(_pool: Any, ids_: list[str]) -> None:
        return None

    async def fake_unlist(_pool: Any, kind: str, listed: list[str]) -> int:
        unlisted.append((kind, listed))
        return 0

    for name, fn in (("content_known", fake_known), ("content_upsert", fake_upsert), ("content_touch", fake_touch),
                     ("content_unlist", fake_unlist)):  # fmt: skip
        monkeypatch.setattr(store, name, fn)
    counts: Counter[str] = Counter()
    await content_sync.refresh(object(), ContentClient(catalog_config(cis)), counts)  # type: ignore[arg-type]
    assert stored == [ids[0], ids[2]] and counts["content_unchanged"] == 1 and counts["content_fetched"] == 2
    assert [k for k, _ in unlisted] == ["prep_pack", "starter_template"]  # campaign blocks are not synced
    # A version CIS no longer serves is skipped; over the fetch budget the rest waits for the next run.
    cis.content[ids[0]]["version"] = 4
    monkeypatch.setattr(content_sync, "MAX_FETCHES", 0)
    counts = Counter()
    await content_sync.refresh(object(), ContentClient(catalog_config(cis)), counts)  # type: ignore[arg-type]
    assert counts["content_deferred"] == 2
    monkeypatch.setattr(content_sync, "MAX_FETCHES", 60)

    async def missing(*_a: Any) -> None:
        return None

    monkeypatch.setattr(ContentClient, "get_content", missing)
    counts = Counter()
    await content_sync.refresh(object(), ContentClient(catalog_config(cis)), counts)  # type: ignore[arg-type]
    assert counts["content_skipped"] == 2
    # CIS down: reported, never raised.
    cis.down = True
    counts = Counter()
    await content_sync.refresh_safely(object(), ContentClient(catalog_config(cis)), counts)  # type: ignore[arg-type]
    assert counts["content_error"] == 1


@pytest.mark.asyncio
async def test_the_content_client_answers() -> None:
    cid = str(uuid.uuid4())
    cis = FakeCis(content={cid: content_body(cid)})
    client = ContentClient(catalog_config(cis))
    assert (await client.get_content(cid, 3)).content_id == cid  # type: ignore[union-attr]
    assert await client.get_content(cid, 9) is None
    items, cursor = await client.list_content("prep_pack", None)
    assert [i.content_id for i in items] == [cid] and cursor is None
    cis.down = True
    with pytest.raises(CatalogUnavailable):
        await client.list_content("prep_pack", "1")
    with pytest.raises(CatalogUnavailable):
        await client.get_content(cid, 3)


@pytest.mark.asyncio
async def test_the_catalog_sync_runs_the_content_pass_after_the_tours(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_run(self: Any, pool: Any, state: Any, counts: Counter[str]) -> bool:
        seen.append("tours")
        return True

    async def fake_refresh(pool: Any, client: Any, counts: Counter[str]) -> None:
        seen.append("content")
        counts["content_fetched"] += 1

    async def fake_state(_pool: Any) -> Any:
        return store.SyncState(None, None, None, 0, None)

    monkeypatch.setattr(CatalogSync, "_run", fake_run)
    monkeypatch.setattr(content_sync, "refresh_safely", fake_refresh)
    monkeypatch.setattr(store, "load_state", fake_state)
    out = await CatalogSync(catalog_config(FakeCis()))(object(), Job("j", "crm_catalog_sync", {}, 1, 3))  # type: ignore[arg-type]
    assert seen == ["tours", "content"] and out == {"status": "pass_done", "content_fetched_count": 1}


@pytest.mark.asyncio
async def test_the_content_store_on_the_pool() -> None:
    pool = ScriptedConn()
    pool.on("SELECT content_id, version, content_hash FROM crm.aa_content", [
        {"content_id": uuid.UUID(int=1), "version": 2, "content_hash": "h"}])  # fmt: skip
    pool.on("UPDATE crm.aa_content SET listed = false", [{"content_id": 1}] * 3)
    assert await store.content_known(pool, "prep_pack") == {str(uuid.UUID(int=1)): (2, "h")}  # type: ignore[arg-type]
    cid = str(uuid.uuid4())
    await store.content_upsert(pool, content(content_body(cid)))  # type: ignore[arg-type]
    args = pool.sql("INSERT INTO crm.aa_content")[0]
    assert args[0] == cid and json.loads(args[7]) == {"country_codes": ["VN"]} and args[9].tzinfo is not None
    await store.content_touch(pool, [])  # type: ignore[arg-type]
    await store.content_touch(pool, [cid])  # type: ignore[arg-type]
    assert len(pool.sql("SET listed = true, checked_at")) == 1
    assert await store.content_unlist(pool, "prep_pack", [cid]) == 3  # type: ignore[arg-type]
