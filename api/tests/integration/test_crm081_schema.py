"""crm-081 (AA-1144, S285 decision D9): crm.option.display_note, one line per item.

The column is nullable text with option_display_note_chk (1..200 characters, no control character); the file is
registered and re-runnable as crm_owner.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import OWNER

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "081_crm_option_display_note.sql"


async def test_display_note_column_and_check(admin: asyncpg.Connection) -> None:
    col = await admin.fetchrow(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'crm' AND table_name = 'option' AND column_name = 'display_note'"
    )
    assert col is not None and col["data_type"] == "text" and col["is_nullable"] == "YES"
    check = await admin.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'crm.option'::regclass AND conname = 'option_display_note_chk'"
    )
    assert check is not None and "200" in check and "cntrl" in check
    assert await admin.fetchval("SELECT has_column_privilege('crm_app', 'crm.option', 'display_note', 'UPDATE')")
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-081'") == 1


@pytest.mark.parametrize(
    "note,ok",
    [(None, True), ("Sea view on request", True), ("x" * 200, True), ("", False), ("x" * 201, False),
     ("two\nlines", False)],
    ids=["null", "one-line", "200", "empty", "201", "newline"],
)  # fmt: skip
async def test_the_check_reads_one_line(admin: asyncpg.Connection, note: str | None, ok: bool) -> None:
    """The CHECK expression itself, on a value (no option row needed)."""
    expr = await admin.fetchval(
        "SELECT pg_get_expr(conbin, conrelid) FROM pg_constraint "
        "WHERE conrelid = 'crm.option'::regclass AND conname = 'option_display_note_chk'"
    )
    got = await admin.fetchval(f"SELECT ({expr.replace('display_note', '$1::text')}) IS NOT FALSE", note)
    assert got is ok


async def test_the_file_is_re_runnable(admin: asyncpg.Connection) -> None:
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'crm.option'::regclass AND conname = 'option_display_note_chk'"
    ) == 1  # fmt: skip
