"""crm-028 (AA-952 CRM-23 slice 4) on PostgreSQL with the RDS role shape.

- Nine new columns on ``crm.invoice``, nullable (``reminder_count`` NOT NULL DEFAULT 0): an existing invoice reads
  them empty, so the code before this key runs unchanged.
- Expand only: no constraint, trigger, policy or grant changed, and the file re-applies as a no-op (idempotent).
- The sealed trigger of crm-024 names its sealed columns, so crm_app writes the new ones on an issued invoice while the
  sealed ones stay refused (CRM31).
- RLS still isolates the rows: another agency neither reads nor writes them.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import OWNER
from tests.integration.test_client_proposal_api import _raw
from tests.integration.test_invoice_claims_api import _deposit_invoice, _text_accepted

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "028_crm_invoice_claims.sql"
COLUMNS = {
    "claimed_at": ("timestamp with time zone", "YES"),
    "hold_extended_at": ("timestamp with time zone", "YES"),
    "hold_extend_code": ("text", "YES"),
    "rehold_requested_at": ("timestamp with time zone", "YES"),
    "rehold_quote_id": ("text", "YES"),
    "rehold_price_hash": ("text", "YES"),
    "rehold_code": ("text", "YES"),
    "last_reminded_at": ("timestamp with time zone", "YES"),
    "reminder_count": ("smallint", "NO"),
}
SHAPE_SQL = """
SELECT (SELECT count(*) FROM pg_constraint WHERE conrelid = 'crm.invoice'::regclass) AS constraints,
       (SELECT count(*) FROM pg_trigger WHERE tgrelid = 'crm.invoice'::regclass AND NOT tgisinternal) AS triggers,
       (SELECT count(*) FROM pg_policy WHERE polrelid = 'crm.invoice'::regclass) AS policies,
       (SELECT relacl::text FROM pg_class WHERE oid = 'crm.invoice'::regclass) AS acl
"""


async def test_the_columns_and_nothing_else(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'crm' AND table_name = 'invoice' AND column_name = ANY($1::text[])",
        list(COLUMNS),
    )
    assert {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows} == COLUMNS
    assert next(r for r in rows if r["column_name"] == "reminder_count")["column_default"] == "0"
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-028'") == 1
    before = dict(await admin.fetchrow(SHAPE_SQL))
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # idempotent: a no-op the second time
    finally:
        await admin.execute("RESET ROLE")
    assert dict(await admin.fetchrow(SHAPE_SQL)) == before
    assert MIGRATION.read_text(encoding="utf-8").startswith("-- migrate: expand\n")


async def test_crm_app_writes_them_on_an_issued_invoice_and_rls_holds(app_pool: asyncpg.Pool) -> None:
    s = await _text_accepted(app_pool)
    other = await _text_accepted(app_pool)
    invoice_id = await _deposit_invoice(app_pool, s)
    await _raw(app_pool, s, "UPDATE crm.invoice SET claimed_at = now(), hold_extend_code = 'extended', "
               "reminder_count = reminder_count + 1, last_reminded_at = now() WHERE invoice_id = $1",
               invoice_id)  # fmt: skip
    row = (await _raw(app_pool, s, "SELECT claimed_at, reminder_count FROM crm.invoice WHERE invoice_id = $1",
                      invoice_id))[0]  # fmt: skip
    assert row["claimed_at"] is not None and row["reminder_count"] == 1
    with pytest.raises(asyncpg.PostgresError) as err:  # a sealed column of an issued invoice stays sealed
        await _raw(app_pool, s, "UPDATE crm.invoice SET memo = 'later' WHERE invoice_id = $1", invoice_id)
    assert err.value.sqlstate == "CRM31"
    assert await _raw(app_pool, other, "SELECT 1 AS ok FROM crm.invoice WHERE invoice_id = $1", invoice_id) == []
    await _raw(app_pool, other, "UPDATE crm.invoice SET reminder_count = 5 WHERE invoice_id = $1", invoice_id)
    count = await _raw(app_pool, s, "SELECT reminder_count FROM crm.invoice WHERE invoice_id = $1", invoice_id)
    assert count[0]["reminder_count"] == 1 and isinstance(invoice_id, uuid.UUID)
