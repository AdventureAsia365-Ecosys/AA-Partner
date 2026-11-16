"""AA-963 (card CRM-32, R35) global search without a database: the accent rule (E), the refusal of a partial identity
search (R), the entitlement filter (role matrix + feature keys), the switch that keeps search off by default, the
shape of one search and the routes. SQL, RLS and the "no plaintext index" proof:
tests/integration/test_aa963_search_db.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

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
    assert sql.count("normalize(") == 2 and "NFKD" in sql and "[\\u0300-\\u036f]" in sql
    assert sql.startswith("strpos(lower(") and "$1" in sql and "name" in sql
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
        ("Ngu*", "partial_search_refused"),
        ("%vui%", "partial_search_refused"),
        ("x" * 121, "query_too_long"),
        ("   ", "invalid_query"),
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
    ],
)
def test_terms_are_classified(raw: str, mode: str, tokens: tuple[str, ...]) -> None:
    term = classify(raw, **LIMITS)
    assert (term.mode, term.name_tokens) == (mode, tokens)


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
    [event] = ctx.audit_events  # one list_view for the clients shown, no contact field
    assert (event.action, event.reason, event.fields, event.object_ids) == (
        "list_view", "global_search", ("display_name",), (str(T1),)
    )  # fmt: skip
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
    assert out == {"mode": "too_short", "kinds": out["kinds"], "groups": []}
    assert not [c for c in conn.calls if "FROM crm." in c[0] and "allowed_features" not in c[0]
                and "setting_value" not in c[0]]  # fmt: skip


@pytest.mark.asyncio
async def test_the_feature_key_adds_whatsapp_templates(on: None) -> None:
    conn = _conn(("whatsapp_sms",)).on("crm.channel_template", [
        {"channel_template_id": T1, "name": "Chào mừng", "language": "vi", "approval": "approved"}
    ])  # fmt: skip
    out = await service.search(tenant_ctx(conn, role="agent"), FakeVault(), "chao")  # type: ignore[arg-type]
    assert {g["kind"]: g["items"][0]["label"] for g in out["groups"]} == {"whatsapp_template": "Chào mừng"}


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
    assert client.post("/crm/v1/search", json={"q": "x" * 201}, headers=h).status_code == 422
    assert client.post("/crm/v1/search", json={"q": "a", "page": 2}, headers=h).status_code == 422
    assert client.get("/crm/v1/search?q=anna", headers=h).status_code == 405  # never a term in a URL


def test_routes_refuse_a_missing_or_foreign_token(client: TestClient, pools: LocalPools) -> None:
    assert client.post("/crm/v1/search", json={"q": "abc"}).status_code == 401
    staff = _headers(pools.cis_admin())
    assert client.post("/crm/v1/search", json={"q": "abc"}, headers=staff).status_code in (401, 403)
