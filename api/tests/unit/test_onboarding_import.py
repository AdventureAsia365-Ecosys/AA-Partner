"""AA-962 (card CRM-31): the import file reader (CSV and the small .xlsx reader), the column rule (**R**: a missing
column is refused before anything is read), the cell cleaners, the onboarding step rules and the routes (who may call
what, the upload cap). The database paths are in tests/integration/test_onboarding_import_api.py. Invented data
only."""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.errors import Invalid, PayloadTooLarge
from app.imports import engine, formats, spec
from app.imports import service as imports
from app.mail.store import MemoryIngestStore
from app.main import create_app
from app.onboarding import STEPS, clean_controller, connections
from app.routers import onboarding as router
from app.travellers import merge
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000a9"


# ── formats ────────────────────────────────────────────────────────────────────────────────────────
def test_csv_with_bom_semicolons_and_blank_rows() -> None:
    data = '﻿Client ID;Full Name;E-mail\nC-1;Mai Lan;mai@example.test\n;;\nC-2;"Bo, Kim";\n'.encode()
    table = formats.read_table(data)
    assert table.format == "csv"
    assert table.headers == ("client_id", "full_name", "e_mail")
    assert [r.number for r in table.rows] == [2, 4]
    assert table.rows[1].cells["full_name"] == "Bo, Kim"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "empty_upload"),
        (b"\n\n", "no_header"),
        (b"a,a\n1,2\n", "duplicate_column"),
        ("name\n".encode("utf-16"), "file_encoding"),
        (b'name\n"unterminated\n', None),
    ],
)
def test_csv_refusals(data: bytes, code: str | None) -> None:
    if code is None:
        formats.read_table(data)  # an open quote at the end of the file is read as one cell, not an error
        return
    with pytest.raises(Invalid) as err:
        formats.read_table(data)
    assert err.value.code == code


def test_size_and_row_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(formats, "MAX_BYTES", 20)
    with pytest.raises(PayloadTooLarge):
        formats.read_table(b"name\n" + b"x" * 30)
    monkeypatch.setattr(formats, "MAX_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(formats, "MAX_ROWS", 2)
    with pytest.raises(Invalid) as err:
        formats.read_table(b"name\na\nb\nc\n")
    assert err.value.code == "too_many_rows"


def _xlsx(rows: list[list[Any]]) -> bytes:
    shared: list[str] = []
    cells_xml = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, str):
                shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        cells_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = f'<?xml version="1.0"?><worksheet {ns}><sheetData>{"".join(cells_xml)}</sheetData></worksheet>'
    strings = "".join(f"<si><t>{s}</t></si>" for s in shared)
    sst = f'<?xml version="1.0"?><sst {ns}>{strings}</sst>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr("xl/sharedStrings.xml", sst)
    return buf.getvalue()


def test_xlsx_first_sheet_with_shared_strings_numbers_and_gaps() -> None:
    data = _xlsx([["Client ref", "Name", "Date of birth"], ["C-9", "Rosa Lind", 32874.0], [], ["C-10", "Ivo Tam"]])
    table = formats.read_table(data)
    assert table.format == "xlsx"
    assert table.headers == ("client_ref", "name", "date_of_birth")
    assert [(r.number, r.cells["name"]) for r in table.rows] == [(2, "Rosa Lind"), (4, "Ivo Tam")]
    assert table.rows[0].cells["date_of_birth"] == "32874"
    assert spec.date("32874") == dt.date(1990, 1, 1)


def test_xlsx_refuses_a_broken_zip_and_an_oversized_member(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Invalid) as err:
        formats.read_table(formats.XLSX_MAGIC + b"not a zip")
    assert err.value.code == "unreadable_file"
    monkeypatch.setattr(formats, "MAX_MEMBER_BYTES", 50)
    with pytest.raises(Invalid) as err:
        formats.read_table(_xlsx([["name"], ["A very long name that inflates the sheet beyond fifty bytes"]]))
    assert err.value.code == "unreadable_file"


# ── spec ───────────────────────────────────────────────────────────────────────────────────────────
def test_card_r_missing_columns_are_named() -> None:
    with pytest.raises(Invalid) as err:
        spec.check_columns(spec.SPECS["trips"], ("trip_ref", "trip_name"))
    assert err.value.code == "missing_columns" and err.value.extra["columns"] == ["client_ref"]
    with pytest.raises(Invalid) as err:
        spec.check_columns(spec.SPECS["notes"], ("text",))
    assert err.value.extra["columns"] == ["client_ref or trip_ref"]
    found = spec.check_columns(spec.SPECS["clients"], ("customer_id", "full_name", "shoe_size"))
    assert found == {"columns": ["client_ref", "name"], "ignored_columns": ["shoe_size"]}


def test_cleaners() -> None:
    assert spec.date("2024-05-01") == spec.date("01/05/2024") == spec.date("1.5.2024") == dt.date(2024, 5, 1)
    assert spec.date("") is None
    with pytest.raises(ValueError):
        spec.date("May 1st")
    assert spec.flag("Có") is True and spec.flag("no") is False and spec.flag("") is None
    assert spec.trip_status("Done") == "completed" and spec.trip_status("canceled") == "cancelled"
    with pytest.raises(ValueError):
        spec.trip_status("booked")
    assert spec.refs("C-1; C-2 |C-1") == ["C-1", "C-2"]
    assert spec.ref("  C   7 ") == "C 7"
    with pytest.raises(ValueError):
        spec.adults("0")


def test_summary_is_codes_and_counts_only() -> None:
    results = [
        engine.Result(2, "create", ("possible_duplicate",)),
        engine.Result(3, "invalid", ("invalid_email",)),
        engine.Result(4, "already_imported"),
    ]
    out = engine.summarise(results)
    assert out["counts"]["create"] == 1 and out["possible_duplicate_count"] == 1
    assert out["issues"] == [
        {"row": 2, "outcome": "create", "codes": ["possible_duplicate"]},
        {"row": 3, "outcome": "invalid", "codes": ["invalid_email"]},
    ]


def test_notes_without_a_ref_get_a_stable_one() -> None:
    cells = {"client_ref": "C-1", "text": "Window seat"}
    assert engine.note_ref(cells) == engine.note_ref(dict(cells)) != engine.note_ref({**cells, "text": "Aisle"})
    assert engine.note_ref({"note_ref": "N 1", **cells}) == "N 1"


# ── onboarding rules ───────────────────────────────────────────────────────────────────────────────
def test_three_required_steps_and_connections_without_secrets() -> None:
    assert [s.key for s in STEPS if s.required] == ["connect_mailbox", "invite_team", "embed_form"]
    facts = {
        "mailboxes": ["disconnected", "connected"],
        "payment_methods": ["manual:active", "stripe:disconnected"],
        "credentials": ["twilio:needs_attention"],
        "calendar_feeds": 0,
    }
    out = {c["key"]: c for c in connections(facts)}
    assert out["mailbox"]["status"] == "connected"
    assert out["card_payments"]["status"] == "disconnected" and out["card_payments"]["bank_transfer"]
    assert out["sms_whatsapp"]["status"] == "needs_attention" and "esign" not in out


def test_controller_settings_are_checked() -> None:
    assert clean_controller(" https://a.example.test/p ", "privacy@a.example.test", "  ") == (
        "https://a.example.test/p",
        "privacy@a.example.test",
        None,
    )
    for args, code in (
        (("ftp://a.example.test", None, None), "invalid_privacy_notice_url"),
        ((None, "two\nlines", None), "invalid_data_request_contact"),
        ((None, None, "x" * 2001), "invalid_consent_text"),
    ):
        with pytest.raises(Invalid) as err:
            clean_controller(*args)
        assert err.value.code == code


# ── routes ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str = "owner") -> Any:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), ingest_store=MemoryIngestStore())
    ctx = tenant_ctx(ScriptedConn(), role=role)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(ctx))
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: object())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return app, headers


def _stub(calls: list[str], name: str) -> Any:
    async def fn(*_: Any, **__: Any) -> Any:
        calls.append(name)
        return {"ok": name}

    return fn


SHA = "a" * 64
ROUTES = [
    ("get", "/crm/v1/onboarding", None, router, "overview", ("owner", "agent", "assistant")),
    ("put", "/crm/v1/onboarding/steps/kickoff_call", {"state": "later"}, router, "set_step", ("owner",)),
    ("put", "/crm/v1/onboarding/controller", {"consent_text": "Yes"}, router, "set_controller", ("owner",)),
    ("post", "/crm/v1/onboarding/acknowledgement", {"version": "v"}, router, "acknowledge", ("owner",)),
    ("post", "/crm/v1/client-imports/preview?entity=clients", b"name\n", imports, "preview", ("owner",)),
    ("post", f"/crm/v1/client-imports?entity=notes&sha256={SHA}", b"text\n", imports, "start", ("owner",)),
    ("get", "/crm/v1/client-imports", None, imports, "recent", ("owner",)),
    ("get", f"/crm/v1/client-imports/{ID}", None, imports, "status", ("owner",)),
    ("get", "/crm/v1/client-duplicates", None, merge, "candidates", ("owner", "agent", "assistant")),
    ("post", f"/crm/v1/travelers/{ID}/merge", {"other_traveler_id": ID}, merge, "merge", ("owner",)),
]


@pytest.mark.parametrize("role", ["owner", "agent", "assistant"])
@pytest.mark.parametrize(("method", "path", "body", "module", "name", "roles"), ROUTES)
def test_each_route_reaches_its_helper_for_its_roles(
    pools: LocalPools,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    method: str,
    path: str,
    body: Any,
    module: Any,
    name: str,
    roles: tuple[str, ...],
) -> None:
    app, headers = _client(pools, monkeypatch, role=role)
    calls: list[str] = []
    monkeypatch.setattr(module, name, _stub(calls, name))
    monkeypatch.setattr(merge, "candidate_count", _stub([], "count"))
    kwargs: dict[str, Any] = {"content": body} if isinstance(body, bytes) else {"json": body}
    with TestClient(app) as client:
        response = client.request(method.upper(), path, headers=headers, **kwargs)
    if role in roles:
        assert response.status_code in (200, 202), response.text
        assert calls == [name]
    else:
        assert response.status_code == 403 and response.json()["detail"] == "role_forbidden"
        assert calls == []


def test_upload_cap_and_query_checks(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers = _client(pools, monkeypatch)
    monkeypatch.setattr(formats, "MAX_BYTES", 10)
    monkeypatch.setattr(imports, "preview", _stub([], "preview"))
    with TestClient(app) as client:
        big = client.post("/crm/v1/client-imports/preview?entity=clients", content=b"x" * 11, headers=headers)
        assert big.status_code == 413 and big.json()["detail"] == "import_too_large"
        bad = client.post("/crm/v1/client-imports/preview?entity=suppliers", content=b"x", headers=headers)
        assert bad.status_code == 422
        region = client.post(
            "/crm/v1/client-imports/preview?entity=clients&phone_region=VNM", content=b"x", headers=headers
        )
        assert region.status_code == 422
        sha = client.post("/crm/v1/client-imports?entity=clients&sha256=xyz", content=b"x", headers=headers)
        assert sha.status_code == 422
