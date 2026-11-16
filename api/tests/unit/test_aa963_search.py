"""AA-963 (card CRM-32, R35) global search without a database: the accent rule (E), the refusal of a partial identity
search (R), the entitlement filter (role matrix + feature keys), the switch that keeps search off by default, the
shape of one search and the routes. SQL, RLS and the "no plaintext index" proof:
tests/integration/test_aa963_search_db.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import platform_settings as ps
from app.entitlements import FEATURES
from app.errors import Forbidden, Invalid
from app.main import create_app
from app.permissions import Action
from app.routers import search as search_router
from app.search import fold, kinds, service
from app.search.query import classify
from tests import settings_serve
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

LIMITS = {"max_chars": 120, "min_text_chars": 2}
T1 = uuid.UUID("00000000-0000-4000-8000-0000000009a1")
T2 = uuid.UUID("00000000-0000-4000-8000-0000000009a2")


# ── E: "Đà Lạt" = "Da Lat" ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Đà Lạt", "Da Lat"),
        ("ĐÀ LẠT", "da lat"),
        ("Hội An", "hoi an"),
        ("Phố cổ Hà Nội", "pho co ha noi"),
        ("Ølsted Łódź", "olsted lodz"),
        ("Straße", "strasse"),
        ("Café Crème", "cafe creme"),
    ],
)
def test_e_accents_and_case_fold_the_same_way(a: str, b: str) -> None:
    assert fold.fold(a) == fold.fold(b) == b.lower()


def test_words_split_on_spaces_and_punctuation() -> None:
    assert fold.words("Đà Lạt – honeymoon, 2027!") == ["da", "lat", "honeymoon", "2027"]
    assert fold.words("  ") == []


def test_the_sql_fold_folds_both_sides_with_one_expression() -> None:
    sql = fold.contains_sql("name", "$1")
    assert sql.count("normalize(") == 3 and "NFKD" in sql and "[\\u0300-\\u036f]" in sql
    assert sql.startswith("(strpos(lower(") and "$1" in sql and "name" in sql
    assert sql.endswith(") <> '')")  # a param that folds to nothing never matches (strpos(x, '') = 1)
    words = fold.all_words_sql("title", "$1")
    assert "@>" in words and "cardinality(" in words
    for expr in (sql, words):  # never a pattern match: a % or _ in the term is plain text
        assert " LIKE " not in expr.upper() and "~" not in expr


# ── R: no partial identity search ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("@example.test", "partial_contact_search"),
        ("hoa@", "partial_contact_search"),
        ("hoa@@example.test", "partial_contact_search"),
        ("+8491", "partial_contact_search"),
        ("+84 91", "partial_contact_search"),
        ("+", "partial_contact_search"),
        ("*@example.test", "partial_search_refused"),
        ("x" * 121, "query_too_long"),
        ("   ", "invalid_query"),
        # Long enough raw, too short once folded: lone combining marks fold to nothing, an NFD "à" to "a".
        ("\u0301\u0301", "query_too_short"),
        ("\u0301 \u0301 \u0301", "query_too_short"),
        ("a\u0300", "query_too_short"),
    ],
)
def test_r_partial_contact_and_wildcards_are_refused(raw: str, code: str) -> None:
    with pytest.raises(Invalid) as err:
        classify(raw, **LIMITS)
    assert err.value.code == code


@pytest.mark.parametrize(
    ("raw", "mode", "tokens"),
    [
        ("Linh.Vo@Example.test", "email", ()),
        ("+84 912 345 678", "phone", ()),
        ("+84912345678", "phone", ()),
        ("Võ Thị  Linh", "text", ("vo", "thi", "linh")),
        ("Đà Lạt", "text", ("da", "lat")),
        ("0912345", "text", ("0912345",)),  # digits without + are a reference, never a partial phone
        ("BK-7Q2", "text", ("bk", "7q2")),
        ("x", "too_short", ()),
        ("a\u0300b", "text", ("ab",)),  # NFD "àb" folds to two letters: long enough
    ],
)
def test_terms_are_classified(raw: str, mode: str, tokens: tuple[str, ...]) -> None:
    term = classify(raw, **LIMITS)
    assert (term.mode, term.name_tokens, term.clients, term.notice) == (mode, tokens, True, None)


@pytest.mark.parametrize(
    ("raw", "notice"),
    [
        ("Ngu*", "partial_search_refused"),
        ("%vui%", "partial_search_refused"),
        ("100% Vietnam", "partial_search_refused"),
        ("Café @ Hội An", "partial_contact_search"),
        ("anna @example.test", "partial_contact_search"),
    ],
)
def test_wildcards_and_a_spaced_at_keep_clients_out_but_search_the_plaintext_kinds(raw: str, notice: str) -> None:
    term = classify(raw, **LIMITS)
    assert (term.mode, term.name_tokens, term.clients, term.notice) == ("text", (), False, notice)


def test_the_bounds_come_from_the_caller() -> None:
    assert classify("ab", max_chars=120, min_text_chars=3).mode == "too_short"
    with pytest.raises(Invalid):
        classify("abcdef", max_chars=5, min_text_chars=1)


# ── Entitlements: role matrix + feature keys, never a plan name ────────────────────────────────────
def test_feature_keys_gate_a_kind() -> None:
    without = [k.code for k in kinds.entitled("agent", [])]
    with_text = [k.code for k in kinds.entitled("agent", ["whatsapp_sms"])]
    assert "whatsapp_template" not in without and "whatsapp_template" in with_text
    assert set(with_text) - set(without) == {"whatsapp_template"}


def test_a_role_the_matrix_denies_never_gets_the_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    owner_only = kinds.Kind("owner_only", Action.TEAM_VIEW, None, kinds.TEXT)
    everyone = kinds.Kind("everyone", Action.TRIP_VIEW, None, kinds.TEXT)
    for role, expected in (("owner", ["owner_only", "everyone"]), ("agent", ["everyone"]), ("assistant", ["everyone"])):
        assert [k.code for k in kinds.entitled(role, [], [owner_only, everyone])] == expected
    assert kinds.entitled("nobody", FEATURES) == []  # an unknown role: fail closed


def test_every_kind_names_a_real_action_feature_and_runner() -> None:
    assert len(set(kinds.CODES)) == len(kinds.CODES)
    assert set(service._RUNNERS) == set(kinds.CODES)
    for k in kinds.KINDS:
        assert isinstance(k.action, Action) and (k.feature is None or k.feature in FEATURES)
        assert k.modes and k.modes <= {"email", "phone", "text"}
    assert [k.code for k in kinds.KINDS if k.modes & {"email", "phone"}] == ["client"]  # only blind indexes


def test_an_unknown_feature_is_a_bug() -> None:
    with pytest.raises(ValueError, match="unknown CRM feature"):
        kinds.entitled("owner", ["teleport"])


# ── The switch: off by default ───────────────────────────────────────────────────────────────────
def test_search_is_off_by_default() -> None:
    d = ps.READER.definition(ps.SEARCH_ENABLED)
    assert ps.get(ps.SEARCH_ENABLED) is False and d.tenant_scoped


@pytest.mark.asyncio
async def test_off_refuses_with_an_audit_event_and_reads_nothing() -> None:
    conn = ScriptedConn()
    ctx = tenant_ctx(conn)
    with pytest.raises(Forbidden) as err:
        await service.search(ctx, FakeVault(), "Da Lat")  # type: ignore[arg-type]
    assert err.value.code == "search_not_enabled"
    [event] = ctx.audit_events
    assert (event.action, event.outcome, event.reason) == ("search", "denied", "search_not_enabled")
    assert not [c for c in conn.calls if "crm.trip" in c[0] or "crm.traveler " in c[0]]


@pytest.mark.asyncio
async def test_config_off_lists_no_kinds() -> None:
    out = await service.config(tenant_ctx(ScriptedConn()))
    assert out["enabled"] is False and out["kinds"] == []
    assert (out["results_per_kind"], out["query_max_chars"], out["min_text_chars"]) == (5, 120, 2)


# ── One search (switch on) ───────────────────────────────────────────────────────────────────────
@pytest.fixture
def on() -> Iterator[None]:
    settings_serve.serve({ps.SEARCH_ENABLED: "true"})
    yield
    settings_serve.reset()


def _conn(features: tuple[str, ...] = ()) -> ScriptedConn:
    return ScriptedConn().on("allowed_features", lambda f: f in features)


@pytest.mark.asyncio
async def test_text_search_runs_only_text_kinds_and_confirms_name_tokens(on: None) -> None:
    v = FakeVault()
    rows = [
        {"traveler_id": T1, "display_name_enc": await v.encrypt("traveler", "display_name_enc", T1, "Võ Thị Linh"),
         "archived_at": None},
        # a 4-byte token collision: decrypted, then dropped (not a match)
        {"traveler_id": T2, "display_name_enc": await v.encrypt("traveler", "display_name_enc", T2, "Lâm Quang"),
         "archived_at": None},
    ]  # fmt: skip
    conn = _conn().on("FROM crm.traveler", rows).on("FROM crm.trip", [
        {"trip_id": T1, "title": "Linh — Đà Lạt", "stage": "inquiry", "start_date": None, "month": None}
    ])  # fmt: skip
    ctx = tenant_ctx(conn, role="assistant")
    out = await service.search(ctx, v, "linh")  # type: ignore[arg-type]
    assert out["mode"] == "text" and "whatsapp_template" not in out["kinds"]
    by_kind = {g["kind"]: g["items"] for g in out["groups"]}
    assert [i["label"] for i in by_kind["client"]] == ["Võ Thị Linh"]
    assert by_kind["trip"][0]["meta"]["stage"] == "inquiry"
    # one list_view per kind shown: the clients (display name only, no contact field) and the trips (pii:travel)
    assert [(e.action, e.object_kind, e.reason, e.fields, e.object_ids) for e in ctx.audit_events] == [
        ("list_view", "traveler", "global_search", ("display_name",), (str(T1),)),
        ("list_view", "trip", "global_search", (), (str(T1),)),
    ]  # fmt: skip
    assert not conn.sql("crm.channel_template")  # the feature key is missing: not even queried
    [(_, _, tokens, limit)] = conn.sql("FROM crm.traveler")
    assert tokens == [b"tok1"] and limit == ps.get(ps.SEARCH_RESULTS_PER_KIND) * service.CANDIDATES_PER_RESULT


@pytest.mark.asyncio
async def test_an_email_searches_clients_only_through_the_blind_index(on: None) -> None:
    conn = _conn(("whatsapp_sms",))
    out = await service.search(tenant_ctx(conn), FakeVault(), "Linh.Vo@Example.test")  # type: ignore[arg-type]
    assert out["mode"] == "email" and out["groups"] == []
    [(email_idx, phone_idx, tokens, _)] = conn.sql("FROM crm.traveler")
    assert (email_idx, phone_idx, tokens) == (b"e:linh.vo@example.test", None, None)
    texts = [c for c in conn.calls if "strpos(" in c[0] or "crm.trip" in c[0]]
    assert texts == []  # no plaintext kind sees an e-mail


@pytest.mark.asyncio
async def test_a_short_term_reads_nothing(on: None) -> None:
    conn = _conn()
    out = await service.search(tenant_ctx(conn), FakeVault(), "x")  # type: ignore[arg-type]
    assert out == {"mode": "too_short", "kinds": out["kinds"], "groups": [], "notice": None}
    assert not [c for c in conn.calls if "FROM crm." in c[0] and "allowed_features" not in c[0]
                and "setting_value" not in c[0]]  # fmt: skip


@pytest.mark.asyncio
async def test_the_feature_key_adds_whatsapp_templates(on: None) -> None:
    conn = _conn(("whatsapp_sms",)).on("crm.channel_template", [
        {"channel_template_id": T1, "name": "Chào mừng", "language": "vi", "approval": "approved"}
    ])  # fmt: skip
    out = await service.search(tenant_ctx(conn, role="agent"), FakeVault(), "chao")  # type: ignore[arg-type]
    assert {g["kind"]: g["items"][0]["label"] for g in out["groups"]} == {"whatsapp_template": "Chào mừng"}


@pytest.mark.asyncio
async def test_a_wildcard_searches_the_plaintext_kinds_and_never_the_clients(on: None) -> None:
    conn = _conn().on("crm.agency_supplier", [
        {"agency_supplier_id": T1, "name": "100% Vietnam Tours", "kind": "own", "category": "tour"}
    ])  # fmt: skip
    out = await service.search(tenant_ctx(conn), FakeVault(), "100% Vietnam")  # type: ignore[arg-type]
    assert (out["mode"], out["notice"]) == ("text", "partial_search_refused")
    assert {g["kind"]: [i["label"] for i in g["items"]] for g in out["groups"]} == {"supplier": ["100% Vietnam Tours"]}
    assert not conn.sql("FROM crm.traveler")  # the client kind is not even queried
    [(term, _)] = conn.sql("FROM crm.agency_supplier")
    assert term == "100% Vietnam"  # strpos on the folded text: the % is a plain character


@pytest.mark.asyncio
async def test_bookings_suppliers_and_services_write_their_list_view(on: None) -> None:
    conn = (
        _conn()
        .on("FROM crm.booking_line", [
            {"booking_line_id": T1, "trip_id": T2, "title": "Pine room", "supplier_kind": "own", "status": "confirmed",
             "trip_title": "Pine weekend"}
        ])
        .on("crm.agency_supplier", [{"agency_supplier_id": T1, "name": "Pine lodge", "kind": "own",
                                     "category": "accommodation"}])
        .on("crm.agency_service", [{"service_id": T2, "name": "Pine walk", "category": "activity", "active": True}])
    )  # fmt: skip
    ctx = tenant_ctx(conn)
    await service.search(ctx, FakeVault(), "pine")  # type: ignore[arg-type]
    assert [(e.action, e.object_kind, e.reason, e.object_ids) for e in ctx.audit_events] == [
        ("list_view", "booking_line", "global_search", (str(T1),)),
        ("list_view", "agency_supplier", "global_search", (str(T1),)),
        ("list_view", "agency_service", "global_search", (str(T2),)),
    ]


def test_a_booking_reference_is_compared_with_its_space_runs_collapsed() -> None:
    assert "regexp_replace(btrim(l.confirmation_no), '[[:space:]]+', ' ', 'g')" in service.BOOKING_SQL
    assert classify("AB    123", **LIMITS).text == "AB 123"  # the term is collapsed the same way


def test_the_audited_kinds_are_kinds() -> None:
    assert set(service.LIST_VIEW_AUDIT) <= set(kinds.CODES)
    assert {"client", "trip", "booking", "supplier", "service"} <= set(service.LIST_VIEW_AUDIT)


# ── The switch fails closed ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def clean_settings() -> Iterator[None]:
    ps.forget_tenant()
    yield
    ps.forget_tenant()
    ps.READER.last_error = None


@pytest.mark.asyncio
async def test_a_failed_agency_settings_read_switches_search_off(clean_settings: None) -> None:
    ctx = tenant_ctx(ScriptedConn().on("WHERE v.tenant_id = $1", [
        {"key": ps.SEARCH_ENABLED, "value_text": "true", "version": 3}
    ]))  # fmt: skip
    assert await service.is_enabled(ctx) is True  # the agency's own approved value
    broken = tenant_ctx(ScriptedConn().on("WHERE v.tenant_id = $1", asyncpg.UndefinedTableError("no table")))
    assert ps.get(ps.SEARCH_ENABLED, TENANT) is True  # the cache still holds the last good rows ...
    assert await service.is_enabled(broken) is False  # ... the switch does not use them
    with pytest.raises(Forbidden) as err:
        await service.search(broken, FakeVault(), "Da Lat")  # type: ignore[arg-type]
    assert err.value.code == "search_not_enabled"


@pytest.mark.asyncio
async def test_an_approval_in_another_task_counts_at_once(clean_settings: None) -> None:
    on_ = tenant_ctx(ScriptedConn().on("WHERE v.tenant_id = $1", [
        {"key": ps.SEARCH_ENABLED, "value_text": "true", "version": 3}
    ]))  # fmt: skip
    assert await service.is_enabled(on_) is True
    off = tenant_ctx(ScriptedConn().on("WHERE v.tenant_id = $1", [
        {"key": ps.SEARCH_ENABLED, "value_text": "false", "version": 4}
    ]))  # fmt: skip
    assert await service.is_enabled(off) is False  # read now, not after the 30 s cache window


@pytest.mark.asyncio
async def test_a_platform_value_from_a_failed_snapshot_refresh_is_off(on: None, clean_settings: None) -> None:
    ctx = tenant_ctx(ScriptedConn())  # the agency has no row of its own: the platform value applies
    assert await service.is_enabled(ctx) is True
    ps.READER.last_error = "TimeoutError: "  # the last refresh failed: the snapshot is the last good one
    assert await service.is_enabled(ctx) is False


# ── Routes ───────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


@pytest.fixture
def client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(search_router, "request_pool", _pool)
    monkeypatch.setattr(search_router, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn())))
    monkeypatch.setattr(search_router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(search_router, "require_keys", lambda keys: keys)
    with TestClient(app) as c:
        yield c


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": str(TENANT)}


def test_routes_reach_the_service_and_keep_the_term_in_the_body(
    client: TestClient, pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def fake_search(ctx: Any, vault: Any, q: str) -> dict[str, Any]:
        seen.append(q)
        return {"mode": "text", "kinds": [], "groups": []}

    async def fake_config(ctx: Any) -> dict[str, Any]:
        return {"enabled": False, "kinds": []}

    monkeypatch.setattr(service, "search", fake_search)
    monkeypatch.setattr(service, "config", fake_config)
    h = _headers(pools.partner_web())
    assert client.get("/crm/v1/search/config", headers=h).json() == {"enabled": False, "kinds": []}
    assert client.post("/crm/v1/search", json={"q": "Đà Lạt"}, headers=h).status_code == 200
    assert seen == ["Đà Lạt"]
    assert client.post("/crm/v1/search", json={"q": "a", "page": 2}, headers=h).status_code == 422
    assert client.get("/crm/v1/search?q=anna", headers=h).status_code == 405  # never a term in a URL
    # A raw term with long space runs is judged by the setting on the collapsed term, not by the body bound.
    assert client.post("/crm/v1/search", json={"q": "a" + " " * 300 + "b"}, headers=h).status_code == 200


SECRET_TERM = "dieu.linh.probe@example.test"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"q": SECRET_TERM + "x" * 1000}, "query_too_long"),
        ({"q": "a", "email": SECRET_TERM}, "invalid_query"),
        ({"q": ["a", SECRET_TERM]}, "invalid_query"),
        ({"term": SECRET_TERM}, "invalid_query"),
        ({"q": ""}, "invalid_query"),
    ],
)
def test_a_refused_body_never_echoes_the_term(
    client: TestClient, pools: LocalPools, caplog: pytest.LogCaptureFixture, body: dict[str, Any], code: str
) -> None:
    with caplog.at_level("DEBUG"):
        res = client.post("/crm/v1/search", json=body, headers=_headers(pools.partner_web()))
    assert res.status_code == 422 and res.json()["detail"] == code
    assert SECRET_TERM not in res.text and "input" not in res.json()
    assert SECRET_TERM not in caplog.text


def test_a_body_that_is_not_json_is_refused_without_echo(client: TestClient, pools: LocalPools) -> None:
    h = {**_headers(pools.partner_web()), "Content-Type": "application/json"}
    res = client.post("/crm/v1/search", content=b'{"q": "' + SECRET_TERM.encode() + b'"', headers=h)
    assert (res.status_code, res.json()["detail"]) == (422, "invalid_query") and SECRET_TERM not in res.text


def test_a_service_refusal_never_echoes_the_term(
    client: TestClient, pools: LocalPools, caplog: pytest.LogCaptureFixture
) -> None:
    settings_serve.serve({ps.SEARCH_ENABLED: "true"})
    try:
        with caplog.at_level("DEBUG"):
            for q, code in (("dieu.linh.probe@", "partial_contact_search"), ("\u0301\u0301", "query_too_short"),
                            ("dieu.linh.probe " * 20, "query_too_long")):  # fmt: skip
                res = client.post("/crm/v1/search", json={"q": q}, headers=_headers(pools.partner_web()))
                assert (res.status_code, res.json()["detail"]) == (422, code), q
                assert "dieu.linh.probe" not in res.text
        assert "dieu.linh.probe" not in caplog.text
    finally:
        settings_serve.reset()


def test_routes_refuse_a_missing_or_foreign_token(client: TestClient, pools: LocalPools) -> None:
    assert client.post("/crm/v1/search", json={"q": "abc"}).status_code == 401
    staff = _headers(pools.cis_admin())
    assert client.post("/crm/v1/search", json={"q": "abc"}, headers=staff).status_code in (401, 403)
