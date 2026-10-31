"""crm-030 (AA-962): an expand file. The import references and the onboarding / controller settings columns exist with
their DM-6.1 tags, the lookup indexes are partial and NOT unique (blind indexes never carry a unique index, crm-003),
and an agency's settings row gets the empty step lists by default."""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

COLUMNS_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS type_name,
       a.attnotnull AS not_null, col_description(c.oid, a.attnum) AS comment
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
 WHERE c.relnamespace = 'crm'::regnamespace AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
   AND (a.attname = 'import_ref_bidx' OR (c.relname = 'tenant_settings' AND (a.attname LIKE 'onboarding_%'
        OR a.attname LIKE 'responsibility_ack_%' OR a.attname IN ('privacy_notice_url', 'data_request_contact',
        'consent_text'))))
 ORDER BY 1, 2
"""


async def test_columns_and_tags(admin: asyncpg.Connection) -> None:
    rows = {(r["table_name"], r["column_name"]): r for r in await admin.fetch(COLUMNS_SQL)}
    for table in ("traveler", "trip", "note"):
        col = rows[(table, "import_ref_bidx")]
        assert col["type_name"] == "bytea" and not col["not_null"]
        assert col["comment"].startswith("pii:")
    assert rows[("tenant_settings", "onboarding_later")]["not_null"]
    assert rows[("tenant_settings", "onboarding_done")]["type_name"] == "text[]"
    assert rows[("tenant_settings", "data_request_contact")]["comment"].startswith("pii:contact")
    assert len(rows) == 3 + 8


async def test_import_ref_indexes_are_partial_and_not_unique(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT indexrelid::regclass::text AS name, indisunique AS uq, pg_get_expr(indpred, indrelid) AS pred "
        "FROM pg_index WHERE indexrelid::regclass::text IN "
        "('crm.traveler_import_ref_idx', 'crm.trip_import_ref_idx', 'crm.note_import_ref_idx')"
    )
    assert len(rows) == 3
    assert all(not r["uq"] and "import_ref_bidx IS NOT NULL" in r["pred"] for r in rows)


async def test_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-030'") == 1
