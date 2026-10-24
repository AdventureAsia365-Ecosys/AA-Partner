"""Stand-ins for the Slice 1 unit tests (AA-936): a transparent vault and contexts on a :class:`ScriptedConn`.

The real SQL, RLS, encryption and dedupe are covered by tests/integration/test_crm005_intake.py on PostgreSQL; these
fakes drive the Python branches around them.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from app.tenancy import IntakeForm, SystemContext, TenantContext
from app.vault import DecryptionDenied
from tests.unit.fakes import ScriptedConn

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
MEMBER = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
OTHER_MEMBER = uuid.UUID("00000000-0000-4000-8000-00000000beef")
FORM_ID = uuid.UUID("00000000-0000-4000-8000-00000000f0f0")
DEFINITION_ID = uuid.UUID("00000000-0000-4000-8000-00000000d0d0")


class FakeVault:
    """``encrypt`` -> ``b"enc:" + table.column.row + value``; ``decrypt`` reverses it, or refuses ``b"bad"``."""

    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.decrypted: list[tuple[str, str]] = []

    async def encrypt(self, table: str, column: str, row_id: Any, value: str | bytes, **_: Any) -> bytes:
        text = value if isinstance(value, str) else value.decode()
        return f"enc:{table}.{column}:{text}".encode()

    async def decrypt(self, table: str, column: str, row_id: Any, blob: bytes, **_: Any) -> str:
        if self.refuse or blob == b"bad":
            raise DecryptionDenied("integrity_check_failed")
        self.decrypted.append((table, column))
        return bytes(blob).decode().split(":", 2)[2]

    async def email_index(self, table: str, column: str, value: str) -> bytes:
        return f"e:{value.strip().lower()}".encode()

    async def phone_index(self, table: str, column: str, value: str, region: str | None = None) -> bytes:
        return f"p:{value}".encode()

    async def name_index(self, table: str, column: str, value: str) -> bytes:
        return b"n" * 8

    async def name_token_indexes(self, table: str, column: str, value: str) -> list[bytes]:
        return [b"tok1"]


def enc(table: str, column: str, value: str) -> bytes:
    return f"enc:{table}.{column}:{value}".encode()


def tenant_ctx(conn: ScriptedConn | None = None, role: str = "owner", member: uuid.UUID = MEMBER) -> TenantContext:
    return TenantContext(conn=conn or ScriptedConn(), tenant_id=TENANT, member_id=member, member_role=role)


def system_ctx(conn: ScriptedConn | None = None) -> SystemContext:
    return SystemContext(conn=conn or ScriptedConn(), tenant_id=TENANT, actor="job:crm_intake_process")


def intake_form(**over: Any) -> IntakeForm:
    base = dict(
        intake_form_id=FORM_ID,
        tenant_id=TENANT,
        label="Website",
        form_definition_id=DEFINITION_ID,
        allowed_origins=("https://www.agency.example",),
        honeypot_field="website",
        default_owner_member_id=None,
        lead_source="website_form",
        privacy_notice_url=None,
    )
    base.update(over)
    return IntakeForm(**base)


def fake_tx(ctx: Any, extra: Any = None):  # noqa: ANN201
    """A replacement for tenant_tx / system_tx / intake_tx that yields ``ctx`` (or ``(ctx, extra)``)."""

    @asynccontextmanager
    async def tx(*_: Any, **__: Any):  # noqa: ANN202
        yield ctx if extra is None else (ctx, extra)

    return tx
