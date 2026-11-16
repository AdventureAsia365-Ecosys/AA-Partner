"""AA-963 (card CRM-32, R35) global search on PostgreSQL with the RDS role shape, as crm_app, with real field
encryption.

Done-when of the card:
- **R**: no plaintext index of personal data exists in schema ``crm`` (the catalogue proof below, with a probe that
  shows the check catches one), and a partial e-mail or phone is refused before anything is read;
- **E**: "Đà Lạt" = "Da Lat" on the database's own fold (trips by whole words, libraries by substring);
- entitlement filtering (feature key), RLS tenant isolation (the same e-mail and title in two agencies), the switch
  per agency (crm.search.enabled through the settings registry, off by default) and one ``list_view`` per search.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import platform_settings as ps
from app.catalogue import service as catalogue
from app.errors import Forbidden, Invalid
from app.ledger import lines, suppliers
from app.mail import templates
from app.platform_settings import service as settings_service
from app.proposals import inclusions
from app.search import fold
from app.search import service as search
from app.tags import service as tags
from app.tenancy import tenant_tx
from app.travellers import service as travellers
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

STAFF = "00000000-0000-4000-8000-00000000a963"
REASON = "zz switch search on for the integration test agency"
# DM-6.1 classes that identify a person or hold free text: never in a plaintext index.
IDENTITY_CLASSES = {
    "contact",
    "name",
    "address",
    "profile",
    "online_id",
    "free_text",
    "payment",
    "government_id",
    "health",
    "emergency_contact",
}
# Every (index, column) pair of a schema, from three sources so no index column escapes the check:
# 1. the key and INCLUDE columns from pg_index.indkey (attnum > 0; 0 marks an expression slot);
# 2. the columns of expressions and predicates of a plain CREATE INDEX (pg_depend on the index);
# 3. the same for an index that backs a PRIMARY KEY / UNIQUE / EXCLUDE constraint: PostgreSQL records those column
#    dependencies on the pg_constraint row (conindid = the index), not on the index itself.
# Each row carries the column's type and its DM-6.1 comment.
INDEX_COLUMNS_SQL = """
WITH idx AS (
  SELECT i.indexrelid, i.indrelid, i.indkey
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = $1
), cols AS (
  SELECT idx.indexrelid, idx.indrelid, k.attnum
    FROM idx CROSS JOIN LATERAL unnest(idx.indkey::int2[]) AS k(attnum)
   WHERE k.attnum > 0
  UNION
  SELECT idx.indexrelid, idx.indrelid, d.refobjsubid
    FROM idx
    JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = idx.indexrelid
                    AND d.refclassid = 'pg_class'::regclass AND d.refobjid = idx.indrelid AND d.refobjsubid > 0
  UNION
  SELECT idx.indexrelid, idx.indrelid, d.refobjsubid
    FROM idx
    JOIN pg_constraint con ON con.conindid = idx.indexrelid AND con.conrelid = idx.indrelid
                          AND con.contype IN ('p', 'u', 'x')
    JOIN pg_depend d ON d.classid = 'pg_constraint'::regclass AND d.objid = con.oid
                    AND d.refclassid = 'pg_class'::regclass AND d.refobjid = idx.indrelid AND d.refobjsubid > 0
)
SELECT DISTINCT cols.indexrelid::regclass::text AS index_name, c.relname AS table_name, a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS column_type, coalesce(col_description(c.oid, a.attnum), '') AS comment
  FROM cols
  JOIN pg_class c ON c.oid = cols.indrelid
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = cols.attnum
 ORDER BY 1, 3
"""
# Operator classes made for partial-text lookups: never on a personal-data column of the schema.
PATTERN_OPCLASSES_SQL = """
SELECT i.indexrelid::regclass::text AS index_name, oc.opcname AS opclass
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = $1
  CROSS JOIN LATERAL unnest(i.indclass::oid[]) AS u(opc)
  JOIN pg_opclass oc ON oc.oid = u.opc
 WHERE oc.opcname ~ '(trgm|pattern_ops|tsvector)'
"""
PLAINTEXT_OK_TYPES = {"date", "smallint", "integer", "character(2)"}  # travel-class dates, room numbers, country codes
# The column types that can hold a name, an e-mail, a phone or free text in clear (and their arrays).
TEXT_LIKE = re.compile(r"^(text|character varying(\(\d+\))?|character(\(\d+\))?|bpchar|citext|jsonb?)(\[\])?$")

# AA-963 review: the proof fails CLOSED. Every indexed text-like column of schema crm carries a DM-6.1 tag (pii:<class>
# or conf:<class>, read case-insensitively after stripping) or is listed below as holding no personal data, with the
# reason. A new indexed text column that is in neither place fails the proof until its author tags it or lists it.
# 1. Column names that are a closed-list code, a technical name or an id in every crm table that indexes them.
NON_PERSONAL_NAMES: dict[str, str] = {
    **dict.fromkeys(
        (
            "status",
            "state",
            "stage",
            "kind",
            "scope",
            "purpose",
            "mode",
            "role",
            "direction",
            "entry_kind",
            "pax_type",
            "step_status",
            "served_state",
            "aa_status",
            "resolution",
            "post_outcome",
            "trigger_kind",
            "from_status",
            "to_status",
            "applies_to",
            "category",
            "channel",
            "locale",
            "provider",
            "source",
            "source_system",
            "contract",
            "table_name",
            "app",
            "model",
        ),
        "a closed-list code or a technical name (CHECK lists, provider names, stage and model names)",
    ),  # fmt: skip
    "object_kind": "audit: the kind of the object an event names (a table or a code), never a value",
    "object_id": "audit: the id of the object an event names, never a value",
    "idempotency_key": "a random request id (a retried call is stored once)",
    "departure_id": "an Adventure Asia departure id (Booking feed)",
    "base": "an ISO 4217 currency code",
    "quote": "an ISO 4217 currency code",
    "country_code": "an ISO 3166 country code of an AA tour",
}
# 2. Single columns: what each holds and why it is not personal data.
NON_PERSONAL_COLUMNS: dict[tuple[str, str], str] = {
    ("aa_catalog_tour", "source_id"): "the AA tour id in the CIS catalogue",
    ("aa_catalog_tour", "title"): "the title of an AA catalogue tour (AA's product)",
    ("aa_content", "group_key"): "the key of an AA content group",
    ("aa_invoice_cache", "number"): "the number of an AA invoice to the agency",
    ("agency_service", "name"): "the name of a service the agency sells (its product)",
    ("agency_supplier", "name"): (
        "the supplier's business or trading name as the agency enters it; the contact person's name and e-mail are "
        "encrypted (pii:name / pii:contact, crm-014). A guide who trades under their own name is a business "
        "counterpart, not a client; the search matches this name as a non-personal field"
    ),
    ("agreement_doc", "version_label"): "the version label of the agency's own terms",
    ("booking_line", "bk_booking_id"): "an AA Booking id",
    ("booking_line", "bk_hold_id"): "an AA Booking hold id",
    ("booking_line", "conf_unique_key"): "the supplier's confirmation number, letters and digits only (crm-014)",
    ("campaign", "aa_tour_ids"): "AA tour ids (crm.aa_catalog_tour source ids)",
    ("cancellation_policy", "name"): "the name of one of the agency's cancellation policies",
    ("channel_template", "content_sid"): "a Twilio Content SID (HX + 32 hex)",
    ("departure_cost", "departure_ref"): "an AA departure id or the agency's own group code",
    ("document", "s3_key"): "a path of ids only (tenants/<id>/documents/<id>), never a file name",
    ("incident_flag", "raised_event_id"): "the id of the Booking event that raised the flag",
    ("inclusion_item", "text_en"): "an 'included / not included' line of the agency's offers (its product)",
    ("ingest_address", "local_part"): "the agency's forward-in address, <slug>-<random> (crm-015)",
    ("intake_form", "public_key"): "a random public key of an intake form",
    ("invoice", "number"): "the agency's invoice number, <prefix>-<sequence> (crm-024)",
    ("member", "cognito_iss"): "the issuer URL of the sign-in token (a pool, the same for every member)",
    ("member", "cognito_sub"): (
        "the random Cognito id of an agency member, the sign-in lookup key on every request (an agency staff "
        "identifier, not a client's; DM-6.1 online_id tagging of (iss, sub) is a follow-up of the AA-963 review)"
    ),
    ("message", "external_id"): "the provider's message id",
    ("message_template", "aa_group_key"): "the key of an AA library template group",
    ("message_template", "title"): "the title of a message template (the body is encrypted)",
    ("messaging_connection", "hook_id"): "32 random hex characters (the agency's webhook path)",
    ("metric_event", "name"): "an event name code (proposal.viewed ...)",
    ("payment", "provider_ref"): "the payment provider's reference of a payment",
    ("payment_session", "txn_ref"): "a random merchant transaction reference",
    ("prep_pack_selection", "group_key"): "the key of an AA prep pack group",
    ("proposal", "hold_set_ref"): "the reference of an AA hold set",
    ("saved_reply", "title"): "the title of a saved reply (agency text; the body is encrypted, crm-015)",
    ("setting_definition", "key"): "a setting key (crm.search.enabled ...)",
    ("setting_value", "key"): "a setting key (crm.search.enabled ...)",
    ("supplier_bill", "number"): "the number of a bill a supplier sent the agency",
    ("support_session", "staff_sub"): (
        "the random WORKFORCE Cognito id of an AA staff member (DM-6.1 online_id tagging is a follow-up of the "
        "AA-963 review)"
    ),
    ("tag", "code"): "the tag's fixed key (^[a-z][a-z0-9_]{0,39}$)",
    ("tour_retired_flag", "raised_key"): "retired:<epoch microseconds>",
    ("tour_retired_flag", "source_id"): "the AA tour id",
    ("trip", "group_code"): "the agency's own code of a group departure",
    ("trip", "mail_token"): "a random mail reference of the trip (crm-015)",
    ("trip_commitment", "source_ref"): "the id of the hold, booking, invoice, payment or signature it comes from",
    ("visa_template", "country"): "the ISO country code a visa template is for",
    ("visa_template", "visa_type"): "a visa type code",
    ("workflow_rule", "template_key"): "the key of an AA library workflow template",
    ("workflow_rule", "trigger_value"): "the stage, event or date anchor code that triggers a rule",
    ("workflow_run", "trigger_key"): "ids and codes of the trigger (stage:<id> | event:<id> | date:...)",
}


def dm61_tag(comment: str) -> str | None:
    """The DM-6.1 tag of a column comment (``pii:<class>`` / ``conf:<class>``), lower-cased, or None. Leading
    white space and case are ignored: `` PII:Contact; ...`` is ``pii:contact``."""
    head = comment.strip().split(";", 1)[0].strip().lower()
    for prefix in ("pii:", "conf:"):
        if head.startswith(prefix):
            return prefix + head[len(prefix) :].strip()
    return None


def plaintext_pii_index_problems(
    rows: list[asyncpg.Record],
    names: Mapping[str, str] = NON_PERSONAL_NAMES,
    columns: Mapping[tuple[str, str], str] = NON_PERSONAL_COLUMNS,
) -> list[str]:
    """Indexed columns that may hold personal data in clear ([] = none). Pure. Fails closed:

    - a column tagged ``pii:`` of an identity / free-text class must be ``bytea`` / ``bytea[]`` (an encrypted value or
      a blind index); any other ``pii:`` column may be indexed only as a date, a number or a two-letter code;
    - a text-like column (text, varchar, char, citext, json, jsonb, or an array of them) with NO DM-6.1 tag must be
      listed in ``names`` or ``columns`` (no personal data, with the reason); otherwise it is a problem."""
    out = []
    for r in rows:
        typ = r["column_type"]
        where = f"{r['index_name']}: {r['table_name']}.{r['column_name']} ({typ}"
        tag = dm61_tag(r["comment"])
        if tag is None:
            listed = r["column_name"] in names or (r["table_name"], r["column_name"]) in columns
            if TEXT_LIKE.match(typ) and not listed:
                out.append(f"{where}, untagged)")
            continue
        if not tag.startswith("pii:") or typ in ("bytea", "bytea[]"):
            continue
        cls = tag[4:]
        if cls in IDENTITY_CLASSES or not (
            typ in PLAINTEXT_OK_TYPES or typ.startswith("timestamp") or typ.startswith("time")
        ):
            out.append(f"{where}, {tag})")
    return out


@pytest_asyncio.fixture(autouse=True)
async def _fresh_reader() -> AsyncIterator[None]:
    ps.invalidate()
    ps.READER._snapshot = None
    yield
    ps.invalidate()
    ps.READER._snapshot = None


# ── R: no plaintext index of personal data ─────────────────────────────────────────────────────────
async def test_r_no_plaintext_index_of_personal_data_in_schema_crm(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(INDEX_COLUMNS_SQL, "crm")
    assert len(rows) > 100  # the catalogue query sees the schema's indexes
    tagged = [r for r in rows if (dm61_tag(r["comment"]) or "").startswith("pii:")]
    assert {(r["table_name"], r["column_name"]) for r in tagged} >= {
        ("traveler", "email_bidx"),
        ("traveler", "phone_bidx"),
        ("traveler", "name_token_bidx"),
    }  # the blind indexes are indexed, as bytea
    assert plaintext_pii_index_problems(rows) == []
    # The allow-list does not rot: every listed column is still an indexed, untagged column of schema crm.
    untagged = {(r["table_name"], r["column_name"]) for r in rows if dm61_tag(r["comment"]) is None}
    assert set(NON_PERSONAL_COLUMNS) - untagged == set()
    assert await admin.fetch(PATTERN_OPCLASSES_SQL, "crm") == []
    # The plaintext personal field the search folds (trip.title, pii:travel) is not indexed at all: a scan inside
    # one agency's rows. (Names of suppliers, templates and tags hold no personal data; their unique keys stay.)
    assert not [r for r in rows if (r["table_name"], r["column_name"]) == ("trip", "title")]


async def test_r_the_index_check_catches_a_plaintext_personal_index(admin: asyncpg.Connection) -> None:
    """The probe of the check itself: a scratch schema with the mistakes the check must name."""
    async with admin.transaction():
        await admin.execute("CREATE SCHEMA aa963_probe")
        await admin.execute(
            "CREATE TABLE aa963_probe.person "
            "(id int, email text, email_bidx bytea, title text, starts date, notes text)"
        )
        for column, tag in (
            ("email", "pii:contact; plaintext by mistake"),
            ("email_bidx", "pii:contact; blind index"),
            ("title", "pii:travel; free title"),
            ("starts", "pii:travel; a date"),
            ("notes", "pii:free_text; agent notes"),
        ):
            await admin.execute(f"COMMENT ON COLUMN aa963_probe.person.{column} IS '{tag}'")
        await admin.execute("CREATE INDEX person_email_idx ON aa963_probe.person (lower(email))")
        await admin.execute("CREATE INDEX person_bidx_idx ON aa963_probe.person (email_bidx)")
        await admin.execute("CREATE INDEX person_title_idx ON aa963_probe.person (title) WHERE starts IS NOT NULL")
        await admin.execute("CREATE INDEX person_notes_idx ON aa963_probe.person (id) WHERE notes IS NOT NULL")
        await admin.execute("CREATE INDEX person_pattern_idx ON aa963_probe.person (title text_pattern_ops)")
        # Constraint-backed indexes: their column dependencies sit on pg_constraint, not on the index.
        await admin.execute("ALTER TABLE aa963_probe.person ADD CONSTRAINT person_email_uq UNIQUE (email)")
        await admin.execute(
            "ALTER TABLE aa963_probe.person ADD CONSTRAINT person_notes_ex EXCLUDE USING btree (lower(notes) WITH =)"
        )
        await admin.execute(
            "CREATE TABLE aa963_probe.guest (id int, email text, CONSTRAINT guest_pk PRIMARY KEY (id) INCLUDE (email))"
        )
        await admin.execute("COMMENT ON COLUMN aa963_probe.guest.email IS 'pii:contact; plaintext by mistake'")
        # Fail closed (AA-963 review): an untagged plaintext e-mail and jsonb, a tag with a leading space and upper
        # case, and an untagged code column the allow-list names (not a problem).
        await admin.execute(
            "CREATE TABLE aa963_probe.contact_card (id int, email text, profile jsonb, note varchar(200), status text)"
        )
        await admin.execute("COMMENT ON COLUMN aa963_probe.contact_card.note IS ' PII:Free_Text; padded tag'")
        await admin.execute("CREATE INDEX card_email_idx ON aa963_probe.contact_card (id, lower(email))")
        await admin.execute("CREATE INDEX card_profile_idx ON aa963_probe.contact_card USING gin (profile)")
        await admin.execute("CREATE INDEX card_note_idx ON aa963_probe.contact_card (note)")
        await admin.execute("CREATE INDEX card_status_idx ON aa963_probe.contact_card (status)")
        problems = plaintext_pii_index_problems(await admin.fetch(INDEX_COLUMNS_SQL, "aa963_probe"))
        patterns = await admin.fetch(PATTERN_OPCLASSES_SQL, "aa963_probe")
        await admin.execute("DROP SCHEMA aa963_probe CASCADE")
    assert problems == [
        "aa963_probe.card_email_idx: contact_card.email (text, untagged)",
        "aa963_probe.card_note_idx: contact_card.note (character varying(200), pii:free_text)",
        "aa963_probe.card_profile_idx: contact_card.profile (jsonb, untagged)",
        "aa963_probe.guest_pk: guest.email (text, pii:contact)",
        "aa963_probe.person_email_idx: person.email (text, pii:contact)",
        "aa963_probe.person_email_uq: person.email (text, pii:contact)",
        "aa963_probe.person_notes_ex: person.notes (text, pii:free_text)",
        "aa963_probe.person_notes_idx: person.notes (text, pii:free_text)",
        "aa963_probe.person_pattern_idx: person.title (text, pii:travel)",
        "aa963_probe.person_title_idx: person.title (text, pii:travel)",
    ]
    assert [r["index_name"] for r in patterns] == ["aa963_probe.person_pattern_idx"]


# ── Fixtures ───────────────────────────────────────────────────────────────────────────────────────
async def _switch_on(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> None:
    await settings_service.propose(
        pool, ps.SEARCH_ENABLED, value=True, reason=REASON, staff_sub=STAFF, base_version=0, tenant_id=tenant_id,
        apply=True,
    )  # fmt: skip


async def _trip(pool: asyncpg.Pool, tenant_id: uuid.UUID, title: str, group_code: str | None = None) -> uuid.UUID:
    trip = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await conn.execute(
            "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, party_adults, group_code) "
            "VALUES ($1, $2, 'inquiry', 'agency', 2, $3)",
            trip,
            title,
            group_code,
        )
    return trip


async def _setup(pool: asyncpg.Pool, *roles: str) -> dict[str, Any]:
    """A synthetic agency with one client, two trips, a supplier with a confirmed line, a template, an inclusion and
    a tag. Invented names only."""
    tenant_id, people = await _agency(pool, *(roles or ("owner",)))
    keys = _keys()
    s: dict[str, Any] = {"tenant": tenant_id, "who": {r: people[i][0] for i, r in enumerate(roles or ("owner",))}}
    s["keys"] = keys
    owner = people[0][0]
    s["dalat"] = await _trip(pool, tenant_id, "Tuần trăng mật Đà Lạt", group_code="GRP-LANG-7")
    s["hue"] = await _trip(pool, tenant_id, "Huế river weekend")
    async with tenant_tx(pool, owner, tenant_id) as ctx:
        v = Vault(keys, ctx)
        client = await travellers.create_traveler(
            ctx, v, {"display_name": "Quách Diệu Linh", "email": "dieu.linh@example.test", "phone": "+84987650123"}
        )
        s["client"] = client["traveler_id"]
        sup = await suppliers.create_supplier(ctx, v, {"name": "Nhà nghỉ Đồi Thông", "category": "accommodation"})
        line = await lines.create_line(
            ctx, s["dalat"], {"agency_supplier_id": uuid.UUID(sup["agency_supplier_id"]), "title": "Pine room"}
        )
        await lines.update_line(ctx, uuid.UUID(line["booking_line_id"]), {"confirmation_no": "PH-88Q1"})
        await templates.create(ctx, v, locale="vi", title="Lời chào Đà Lạt", body="Xin chào {client_first_name}")
        await inclusions.create_item(ctx, {"kind": "included", "text_en": "Đà Lạt flower garden ticket"})
        await tags.create(ctx, "trip", "Đà Lạt lovers")
    return s


async def _search(pool: asyncpg.Pool, s: dict[str, Any], q: str, role: str = "owner") -> dict[str, Any]:
    async with tenant_tx(pool, s["who"][role], s["tenant"]) as ctx:
        return await search.search(ctx, Vault(s["keys"], ctx), q)


def _labels(out: dict[str, Any]) -> dict[str, list[str]]:
    return {g["kind"]: [i["label"] for i in g["items"]] for g in out["groups"]}


# ── The switch ─────────────────────────────────────────────────────────────────────────────────────
async def test_off_by_default_then_on_for_one_agency_only(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    s = await _setup(app_pool)
    other = await _setup(app_pool)
    with pytest.raises(Forbidden) as err:
        await _search(app_pool, s, "Da Lat")
    assert err.value.code == "search_not_enabled"
    denied = await _owner_fetch(admin, s["tenant"], "SELECT reason FROM crm.audit_event WHERE action = 'search'")
    assert [r["reason"] for r in denied] == ["search_not_enabled"]
    await _switch_on(app_pool, s["tenant"])
    assert (await _search(app_pool, s, "Da Lat"))["groups"]
    with pytest.raises(Forbidden):  # another agency keeps the platform value: off
        await _search(app_pool, other, "Da Lat")
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        assert (await search.config(ctx))["enabled"] is True


# ── E: "Đà Lạt" = "Da Lat" ─────────────────────────────────────────────────────────────────────────
async def test_e_accent_insensitive_on_the_database_fold(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    await _switch_on(app_pool, s["tenant"])
    for q in ("Da Lat", "da lat", "ĐÀ LẠT", "Đà Lạt"):
        got = _labels(await _search(app_pool, s, q))
        assert got["trip"] == ["Tuần trăng mật Đà Lạt"], q
        assert got["message_template"] == ["Lời chào Đà Lạt"], q
        assert got["inclusion"] == ["Đà Lạt flower garden ticket"], q
        assert got["tag"] == ["Đà Lạt lovers"], q
    assert _labels(await _search(app_pool, s, "doi thong"))["supplier"] == ["Nhà nghỉ Đồi Thông"]
    # A trip title (pii:travel) matches whole words only: a fragment never probes it.
    assert "trip" not in _labels(await _search(app_pool, s, "La"))
    assert "trip" not in _labels(await _search(app_pool, s, "trang ma"))
    assert _labels(await _search(app_pool, s, "trang mat"))["trip"] == ["Tuần trăng mật Đà Lạt"]


async def test_e_the_sql_fold_equals_the_python_fold(admin: asyncpg.Connection) -> None:
    samples = ["Đà Lạt", "ĐÀ LẠT", "Hội An", "Ølsted Łódź", "Straße", "Café Crème", "Phú Quốc – 2027!"]
    for text in samples:
        got = await admin.fetchval(f"SELECT {fold.fold_sql('$1::text')}", text)  # noqa: S608 — fixed expression
        words = await admin.fetchval(f"SELECT {fold.words_sql('$1::text')}", text)  # noqa: S608
        assert got == fold.fold(text), text
        assert words == fold.words(text), text


# ── Clients through blind indexes only; references exact ──────────────────────────────────────────
async def test_clients_by_whole_email_phone_or_name_words_and_one_list_view(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _setup(app_pool)
    await _switch_on(app_pool, s["tenant"])
    for q in ("DIEU.LINH@example.test", "+84 987 650 123", "linh quach", "Diệu Linh"):
        out = await _search(app_pool, s, q)
        assert _labels(out).get("client") == ["Quách Diệu Linh"], q
        assert out["groups"][0]["items"][0]["id"] == s["client"]
    assert "client" not in _labels(await _search(app_pool, s, "Lin"))  # a name fragment: no token, no match
    for q in ("dieu.linh@", "@example.test", "+8498765"):
        with pytest.raises(Invalid) as err:
            await _search(app_pool, s, q)
        assert err.value.code == "partial_contact_search"
    views = await _owner_fetch(
        admin,
        s["tenant"],
        "SELECT fields, reason, object_ids FROM crm.audit_event"
        " WHERE action = 'list_view' AND object_kind = 'traveler' ORDER BY at, event_id",
    )
    assert [(r["fields"], r["reason"], r["object_ids"]) for r in views] == [
        (["display_name"], "global_search", [s["client"]])
    ] * 4


async def test_bookings_by_exact_reference_and_trips_by_group_code(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    await _switch_on(app_pool, s["tenant"])
    out = await _search(app_pool, s, "ph-88q1")
    [booking] = next(g["items"] for g in out["groups"] if g["kind"] == "booking")
    assert (booking["label"], booking["meta"]["trip_id"]) == ("Pine room", str(s["dalat"]))
    assert "booking" not in _labels(await _search(app_pool, s, "PH-88"))  # a reference is exact
    assert _labels(await _search(app_pool, s, "grp-lang-7"))["trip"] == ["Tuần trăng mật Đà Lạt"]


# ── Entitlements and RLS ───────────────────────────────────────────────────────────────────────────
async def test_the_feature_key_decides_the_whatsapp_templates_kind(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _setup(app_pool, "owner", "agent", "assistant")
    await _switch_on(app_pool, s["tenant"])
    for role in ("owner", "agent", "assistant"):
        out = await _search(app_pool, s, "Da Lat", role)
        assert "whatsapp_template" not in out["kinds"] and "client" in out["kinds"], role
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await admin.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await admin.execute(
            "INSERT INTO crm.tenant_entitlement (tenant_id, allowed_features) VALUES ($1, ARRAY['whatsapp_sms']) "
            "ON CONFLICT (tenant_id) DO UPDATE SET allowed_features = EXCLUDED.allowed_features",
            s["tenant"],
        )
    out = await _search(app_pool, s, "Da Lat", "assistant")
    assert "whatsapp_template" in out["kinds"]


async def test_rls_another_agency_with_the_same_email_and_title_is_never_found(app_pool: asyncpg.Pool) -> None:
    a = await _setup(app_pool)
    b = await _setup(app_pool)
    for s in (a, b):
        await _switch_on(app_pool, s["tenant"])
    for q in ("dieu.linh@example.test", "Da Lat", "ph-88q1", "Quách"):
        got_a = await _search(app_pool, a, q)
        got_b = await _search(app_pool, b, q)
        ids_a = {i["id"] for g in got_a["groups"] for i in g["items"]}
        ids_b = {i["id"] for g in got_b["groups"] for i in g["items"]}
        assert ids_a and ids_b and not ids_a & ids_b, q
        assert a["client"] not in ids_b and str(a["dalat"]) not in ids_b, q


# ── AA-963 review fixes ──────────────────────────────────────────────────────────────────────────────
async def test_a_term_that_folds_to_nothing_is_refused_and_the_sql_fails_safe(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _setup(app_pool)
    await _switch_on(app_pool, s["tenant"])
    for q in ("\u0301\u0301", "a\u0300"):  # lone combining marks fold to nothing, an NFD "à" to one letter
        with pytest.raises(Invalid) as err:
            await _search(app_pool, s, q)
        assert err.value.code == "query_too_short", q
    # The SQL's own guard: an empty folded needle never matches (strpos(x, '') = 1 without it).
    sql = f"SELECT {fold.contains_sql('$1::text', '$2::text')} AS hit"  # noqa: S608 — fixed expression
    assert await admin.fetchval(sql, "Nhà nghỉ Đồi Thông", "\u0301\u0301") is False
    assert await admin.fetchval(sql, "Nhà nghỉ Đồi Thông", "doi") is True


async def test_wildcards_and_a_spaced_at_search_the_plaintext_kinds_only(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    await _switch_on(app_pool, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        v = Vault(s["keys"], ctx)
        await suppliers.create_supplier(ctx, v, {"name": "100% Lantern Valley", "category": "activity"})
        await suppliers.create_supplier(ctx, v, {"name": "Quách @ Pine Hill", "category": "accommodation"})
    out = await _search(app_pool, s, "100% lantern")
    assert (out["notice"], _labels(out)) == ("partial_search_refused", {"supplier": ["100% Lantern Valley"]})
    out = await _search(app_pool, s, "Quách @ Pine")  # the client is named Quách: never searched through an "@" term
    assert (out["notice"], _labels(out)) == ("partial_contact_search", {"supplier": ["Quách @ Pine Hill"]})
    with pytest.raises(Invalid) as err:
        await _search(app_pool, s, "*@example.test")
    assert err.value.code == "partial_search_refused"


async def test_a_booking_reference_with_space_runs_is_found(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    await _switch_on(app_pool, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        v = Vault(s["keys"], ctx)
        sup = await suppliers.create_supplier(ctx, v, {"name": "Lantern boats", "category": "transport"})
        line = await lines.create_line(
            ctx, s["hue"], {"agency_supplier_id": uuid.UUID(sup["agency_supplier_id"]), "title": "River boat"}
        )
        await lines.update_line(ctx, uuid.UUID(line["booking_line_id"]), {"confirmation_no": "RB   42  K"})
    for q in ("rb 42 k", "RB   42  K"):
        assert _labels(await _search(app_pool, s, q)).get("booking") == ["River boat"], q
    assert "booking" not in _labels(await _search(app_pool, s, "rb 42"))  # still exact


async def test_every_kind_with_a_list_audit_writes_one_per_search(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _setup(app_pool)
    await _switch_on(app_pool, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        made = await catalogue.create_service(
            ctx,
            {"category": "fee", "name": "Pine room upgrade", "unit_sell": "25", "currency": "USD",
             "qty_basis": "per_booking"},
        )  # fmt: skip
    await _search(app_pool, s, "Đà Lạt")  # trip, template, inclusion, tag
    await _search(app_pool, s, "ph-88q1")  # booking line
    await _search(app_pool, s, "upgrade")  # service
    await _search(app_pool, s, "doi thong")  # supplier
    rows = await _owner_fetch(
        admin,
        s["tenant"],
        "SELECT object_kind, reason, object_ids FROM crm.audit_event WHERE action = 'list_view' ORDER BY at, event_id",
    )
    seen = {(r["object_kind"], r["reason"]) for r in rows}
    assert seen == {
        ("trip", "global_search"),
        ("inclusion_item", "global_search"),
        ("booking_line", "global_search"),
        ("agency_service", "global_search"),
        ("agency_supplier", "global_search"),
    }
    service_ids = [r["object_ids"] for r in rows if r["object_kind"] == "agency_service"]
    assert service_ids == [[made["service_id"]]]
    trip_ids = [r["object_ids"] for r in rows if r["object_kind"] == "trip"]
    assert trip_ids == [[str(s["dalat"])]]


async def test_a_failed_settings_read_switches_search_off_on_the_database(
    app_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await _setup(app_pool)
    await _switch_on(app_pool, s["tenant"])
    assert (await _search(app_pool, s, "Da Lat"))["groups"]  # on, and cached in this process
    # The agency's settings read now fails (a statement error, as a grant or RLS change would cause).
    monkeypatch.setattr(ps, "TENANT_SQL", "SELECT * FROM crm.no_such_settings_table WHERE $1::uuid IS NOT NULL")
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        assert await search.is_enabled(ctx) is False
        with pytest.raises(Forbidden) as err:
            await search.search(ctx, Vault(s["keys"], ctx), "Da Lat")
        assert err.value.code == "search_not_enabled"
        assert await ctx.conn.fetchval("SELECT 1 AS ok") == 1  # the savepoint kept the request's transaction usable
