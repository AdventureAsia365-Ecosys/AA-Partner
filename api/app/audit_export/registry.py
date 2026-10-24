"""Platform calls of the audit export (crm-007 functions, no tenant rows): the agency list, the partition maker, the
verified drop, the partition floor. Listed in the DB access lint's platform modules (``tests/unit/test_code_rules.py``)
like the job queue; everything that reads an agency's audit goes through :func:`app.tenancy.audit_export_tx`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg

AGENCIES_SQL = (
    "SELECT tenant_id, exported_until, verified_until, last_seq, registered_at FROM crm.audit_export_agencies()"
)
ENSURE_SQL = "SELECT crm.audit_ensure_partitions()"
DROP_SQL = "SELECT partition_name, range_end, action FROM crm.audit_drop_expired_partitions($1)"


@dataclass(frozen=True)
class Agency:
    tenant_id: uuid.UUID
    exported_until: datetime
    verified_until: datetime | None
    last_seq: int
    registered_at: datetime | None = None


@dataclass(frozen=True)
class PartitionAction:
    partition_name: str
    range_end: datetime
    action: str  # dropped | would_drop | kept_unverified


async def agencies(pool: asyncpg.Pool) -> list[Agency]:
    return [Agency(**dict(r)) for r in await pool.fetch(AGENCIES_SQL)]


async def ensure_partitions(pool: asyncpg.Pool) -> int:
    return int(await pool.fetchval(ENSURE_SQL))


async def drop_expired_partitions(pool: asyncpg.Pool, *, dry_run: bool) -> list[PartitionAction]:
    return [PartitionAction(**dict(r)) for r in await pool.fetch(DROP_SQL, dry_run)]
