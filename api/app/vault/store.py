"""The DEK store: where wrapped tenant DEKs live (design v3 §3.1 "DEK store: crm.tenant_key on Dev; S3 with
retention <= 30 days if prod shares an RDS").

:class:`DekStore` is the pluggable interface; :class:`TableDekStore` is the Dev store over ``crm.tenant_key``
(crm-003). Every call takes the request's :class:`~app.tenancy.TenantContext` and runs on its connection, so the
row-level security of ``tenant_tx()`` applies: a store call can only ever see the caller's agency. This module is one
of the DB modules of the access lint (``tests/unit/test_code_rules.py``) for that reason.

The store holds wrapped DEKs only. Unwrapping is the provider's job (:mod:`app.vault.provider`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from app.tenancy import TenantContext


@dataclass(frozen=True)
class KeyRow:
    purpose: str
    key_version: int
    kek_provider: str
    kek_id: str
    wrapped_dek: bytes | None
    retired: bool
    destroyed: bool

    def __repr__(self) -> str:  # never print the wrapped blob
        return f"KeyRow({self.purpose!r}, v{self.key_version}, {self.kek_provider}, retired={self.retired})"


class DekStore(Protocol):
    async def current(self, ctx: TenantContext, purpose: str) -> KeyRow | None: ...

    async def version(self, ctx: TenantContext, purpose: str, key_version: int) -> KeyRow | None: ...

    async def oldest_live(self, ctx: TenantContext, purpose: str) -> KeyRow | None: ...

    async def insert(
        self, ctx: TenantContext, purpose: str, key_version: int, kek_provider: str, kek_id: str, wrapped: bytes
    ) -> bool:
        """Store a new key; ``False`` when that ``(purpose, version)`` or a current key already exists (a race)."""
        ...

    async def retire_current(self, ctx: TenantContext, purpose: str) -> KeyRow | None:
        """Lock and retire the current key; returns it (``None`` when there is none)."""
        ...

    async def live(self, ctx: TenantContext) -> list[KeyRow]:
        """Every not-destroyed key of the tenant, locked for update (rewrap)."""
        ...

    async def replace_wrapping(
        self, ctx: TenantContext, purpose: str, key_version: int, kek_provider: str, kek_id: str, wrapped: bytes
    ) -> None: ...

    async def destroy_all(self, ctx: TenantContext) -> int: ...


_COLUMNS = (
    "purpose, key_version, kek_provider, kek_id, wrapped_dek, retired_at IS NOT NULL AS retired, "
    "destroyed_at IS NOT NULL AS destroyed"
)


def _row(record) -> KeyRow | None:  # noqa: ANN001 — asyncpg.Record
    if record is None:
        return None
    return KeyRow(
        record["purpose"],
        record["key_version"],
        record["kek_provider"],
        record["kek_id"],
        None if record["wrapped_dek"] is None else bytes(record["wrapped_dek"]),
        record["retired"],
        record["destroyed"],
    )


def context_json(ctx: TenantContext, purpose: str) -> str:
    return json.dumps({"tenant_id": str(ctx.tenant_id), "purpose": purpose})


class TableDekStore:
    """``crm.tenant_key`` through the tenant context's connection (RLS: this agency's rows only)."""

    async def current(self, ctx: TenantContext, purpose: str) -> KeyRow | None:
        return _row(
            await ctx.conn.fetchrow(
                f"SELECT {_COLUMNS} FROM crm.tenant_key WHERE purpose = $1 AND retired_at IS NULL",  # noqa: S608
                purpose,
            )
        )

    async def version(self, ctx: TenantContext, purpose: str, key_version: int) -> KeyRow | None:
        return _row(
            await ctx.conn.fetchrow(
                f"SELECT {_COLUMNS} FROM crm.tenant_key WHERE purpose = $1 AND key_version = $2",  # noqa: S608
                purpose,
                key_version,
            )
        )

    async def oldest_live(self, ctx: TenantContext, purpose: str) -> KeyRow | None:
        return _row(
            await ctx.conn.fetchrow(
                f"SELECT {_COLUMNS} FROM crm.tenant_key WHERE purpose = $1 AND destroyed_at IS NULL "  # noqa: S608
                "ORDER BY key_version LIMIT 1",
                purpose,
            )
        )

    async def insert(
        self, ctx: TenantContext, purpose: str, key_version: int, kek_provider: str, kek_id: str, wrapped: bytes
    ) -> bool:
        inserted = await ctx.conn.fetchval(
            "INSERT INTO crm.tenant_key (purpose, key_version, kek_provider, kek_id, wrapped_dek, encryption_context) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT DO NOTHING RETURNING key_version",
            purpose,
            key_version,
            kek_provider,
            kek_id,
            wrapped,
            context_json(ctx, purpose),
        )
        return inserted is not None

    async def retire_current(self, ctx: TenantContext, purpose: str) -> KeyRow | None:
        return _row(
            await ctx.conn.fetchrow(
                "UPDATE crm.tenant_key SET retired_at = now() WHERE purpose = $1 AND retired_at IS NULL "  # noqa: S608
                f"RETURNING {_COLUMNS}",
                purpose,
            )
        )

    async def live(self, ctx: TenantContext) -> list[KeyRow]:
        records = await ctx.conn.fetch(
            f"SELECT {_COLUMNS} FROM crm.tenant_key WHERE destroyed_at IS NULL "  # noqa: S608
            "ORDER BY purpose, key_version FOR UPDATE"
        )
        return [r for r in (_row(x) for x in records) if r is not None]

    async def replace_wrapping(
        self, ctx: TenantContext, purpose: str, key_version: int, kek_provider: str, kek_id: str, wrapped: bytes
    ) -> None:
        await ctx.conn.execute(
            "UPDATE crm.tenant_key SET kek_provider = $3, kek_id = $4, wrapped_dek = $5, "
            "rewrapped_at = clock_timestamp() WHERE purpose = $1 AND key_version = $2 AND destroyed_at IS NULL",
            purpose,
            key_version,
            kek_provider,
            kek_id,
            wrapped,
        )

    async def destroy_all(self, ctx: TenantContext) -> int:
        status = await ctx.conn.execute(
            "UPDATE crm.tenant_key SET wrapped_dek = NULL, destroyed_at = now(), "
            "retired_at = coalesce(retired_at, now()) WHERE destroyed_at IS NULL"
        )
        return int(str(status).rsplit(" ", 1)[-1]) if str(status).startswith("UPDATE") else 0
