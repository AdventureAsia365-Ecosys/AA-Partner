"""crm.job conforms to the job table DDL template (ADR 0003a decision 2; card CRM-05 "shared conformance tests").

The template and the comparator are byte copies of AA-CIS-App ``shared/jobs/`` (``tests/job_template/``, drift-checked
by the CI job "Vendored CIS copies"). The template is rendered with the CRM placeholders into a scratch schema inside a
transaction that is rolled back, then both tables are read from pg_catalog and compared: columns, CHECKs, PK / FK,
indexes by definition, the updated_at trigger and reloptions. No extra index is allowed (crm-001 is the plain template).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from tests.job_template.job_table_shape import compare, load_template, read_shape, render

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SCRATCH = "x6_tmpl"
CRM_KIND_PATTERN = r"^crm_[a-z0-9_]+$"  # owners.yaml job_kind_prefix crm_ (ADR 0003a decision 3)


@pytest_asyncio.fixture
async def scratch(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(f"CREATE SCHEMA {SCRATCH}")
        await conn.execute(
            render(
                load_template(),
                schema=SCRATCH,
                kind_pattern=CRM_KIND_PATTERN,
                extra_statuses="",
                max_attempts_default=5,
            )
        )
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def test_crm_job_matches_the_template(scratch: asyncpg.Connection) -> None:
    diffs = compare(await read_shape(scratch, SCRATCH), await read_shape(scratch, "crm"))
    assert diffs == [], "\n".join(diffs)


async def test_the_comparison_reports_drift(scratch: asyncpg.Connection) -> None:
    """Not vacuous: a missing template index and an unlisted extra index both show up."""
    await scratch.execute("SET ROLE crm_owner")
    await scratch.execute("DROP INDEX crm.job_running_idx")
    await scratch.execute("CREATE INDEX zz_x6_extra ON crm.job (created_by)")
    await scratch.execute("RESET ROLE")
    diffs = "\n".join(compare(await read_shape(scratch, SCRATCH), await read_shape(scratch, "crm")))
    assert "index missing: USING btree (kind, locked_until) WHERE (status = 'running'::text)" in diffs
    assert "index not in template: USING btree (created_by)" in diffs
