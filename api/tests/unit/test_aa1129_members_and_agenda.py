"""AA-1129 (S283 UI audit, agency app P1 / P2): the members list names people (the invitation address before the "@",
never the full address, never a bare id) and says which of its role a member is; the vault opens only the invited
members' addresses and one ``list_view`` event names them. Invented addresses only."""

from __future__ import annotations

import asyncio
import uuid

from app import pipeline
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, FakeVault, enc, tenant_ctx

INVITATION = uuid.UUID("00000000-0000-4000-8000-0000000011aa")
THIRD = uuid.UUID("00000000-0000-4000-8000-0000000033cc")


def test_display_name_is_the_local_part_only() -> None:
    assert pipeline.display_name("lan.nguyen@hoasen.example") == "lan.nguyen"
    assert pipeline.display_name("  ") is None
    assert pipeline.display_name(None) is None
    assert pipeline.display_name("@nolocal.example") is None
    assert pipeline.display_name("x" * 80 + "@long.example") == "x" * 64


def test_members_carry_a_name_and_a_place_in_their_role() -> None:
    conn = ScriptedConn()
    conn.on(
        "FROM crm.member m",
        [
            {"member_id": MEMBER, "role": "agent", "status": "active", "seq": 1, "invitation_id": INVITATION,
             "email_enc": enc("member_invitation", "email_enc", "lan.nguyen@hoasen.example")},
            {"member_id": OTHER_MEMBER, "role": "agent", "status": "active", "seq": 2, "invitation_id": None,
             "email_enc": None},
            {"member_id": THIRD, "role": "owner", "status": "active", "seq": 1, "invitation_id": None,
             "email_enc": None},
        ],
    )  # fmt: skip
    ctx = tenant_ctx(conn, role="agent", member=OTHER_MEMBER)
    vault = FakeVault()
    out = asyncio.run(pipeline.members(ctx, vault))
    assert [(m["display_name"], m["role"], m["seq"], m["is_me"]) for m in out] == [
        ("lan.nguyen", "agent", 1, False),
        (None, "agent", 2, True),
        (None, "owner", 1, False),
    ]
    assert "@" not in str(out)  # never the full address
    assert vault.decrypted == [("member_invitation", "email_enc")]
    events = [e for e in ctx.audit_events if e.action == "list_view"]
    assert len(events) == 1 and events[0].object_ids == (str(MEMBER),) and events[0].fields == ("email_enc",)


def test_no_address_opened_means_no_audit_event() -> None:
    conn = ScriptedConn()
    conn.on("FROM crm.member m", [{"member_id": MEMBER, "role": "owner", "status": "active", "seq": 1,
                                   "invitation_id": None, "email_enc": None}])  # fmt: skip
    ctx = tenant_ctx(conn)
    assert asyncio.run(pipeline.members(ctx, FakeVault()))[0]["display_name"] is None
    assert not [e for e in ctx.audit_events if e.action == "list_view"]


def test_the_members_sql_ranks_within_the_role_and_reads_the_accepted_invitation() -> None:
    sql = pipeline.MEMBERS_SQL
    assert "row_number() OVER (PARTITION BY m.role ORDER BY m.created_at, m.member_id)" in sql
    assert "inv.status = 'accepted'" in sql and "m.deleted_at IS NULL" in sql


# ── the Unmatched inbox: who wrote, about what (S283 audit row 4) ───────────────────────────────────────────────────
UNMATCHED = uuid.UUID("00000000-0000-4000-8000-0000000044dd")
QUIET = uuid.UUID("00000000-0000-4000-8000-0000000055ee")
INBOUND = uuid.UUID("00000000-0000-4000-8000-0000000066ff")


def test_unmatched_threads_show_sender_subject_and_a_short_preview() -> None:
    import datetime as dt

    from app.mail import conversations

    now = dt.datetime(2031, 3, 1, tzinfo=dt.UTC)
    base = {"trip_id": None, "traveler_id": None, "channel": "email", "last_message_at": now, "message_count": 2,
            "held_count": 0, "last_direction": "in"}  # fmt: skip
    conn = ScriptedConn()
    conn.on("FROM crm.conversation c", [base | {"conversation_id": UNMATCHED}, base | {"conversation_id": QUIET}])
    body = "Hello,   we would like a 5-day trip\nto the invented hills " + "for a family of four " * 10
    conn.on(
        "DISTINCT ON (conversation_id)",
        [{"conversation_id": UNMATCHED, "message_id": INBOUND,
          "from_enc": enc("message", "from_enc", "Minh Tran <minh@guest.example>"),
          "subject_enc": enc("message", "subject_enc", "Family trip in May"),
          "body_enc": enc("message", "body_enc", body)}],
    )  # fmt: skip
    ctx = tenant_ctx(conn)
    out = asyncio.run(conversations.list_conversations(ctx, unmatched=True, vault=FakeVault()))["conversations"]
    first, quiet = out
    assert first["sender"] == "Minh Tran <minh@guest.example>" and first["subject"] == "Family trip in May"
    assert first["preview"].startswith("Hello, we would like a 5-day trip to the invented hills")
    assert len(first["preview"]) == conversations.PREVIEW_CHARS and first["preview"].endswith("…")
    assert (quiet["sender"], quiet["subject"], quiet["preview"]) == (None, None, None)
    views = [(e.object_kind, e.object_ids) for e in ctx.audit_events if e.action == "list_view"]
    assert views == [("conversation", (str(UNMATCHED), str(QUIET))), ("message", (str(INBOUND),))]


def test_other_lists_open_nothing() -> None:
    from app.mail import conversations

    conn = ScriptedConn().on("FROM crm.conversation c", [])
    ctx = tenant_ctx(conn)
    assert asyncio.run(conversations.list_conversations(ctx, unmatched=True, vault=FakeVault())) == {
        "conversations": []
    }
    assert all("DISTINCT ON" not in call[0] for call in conn.calls)
