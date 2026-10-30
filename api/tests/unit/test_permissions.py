"""The permission matrix (AA-939, design v3 §4.6), tested exhaustively against the design table written out again here.

The expected table below is copied from the design (v3 §4.6 / final §5.5), not derived from ``app.permissions``: a
change to either side fails a cell. Card R tests: an agent export or client delete -> 403; a refund above the
threshold by an agent -> 403.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.errors import Forbidden
from app.permissions import MATRIX, ROLES, Action, Rule, decide, may, require, rule_for
from tests.unit.slice_fakes import tenant_ctx

Y, N, OWN, ASG, THR = "Y", "N", "own", "assigned", "threshold"

# design §4.6 row -> (owner, agent, assistant)
DESIGN: dict[str, tuple[str, str, str]] = {
    "session_view": (Y, Y, Y),
    "pipeline_view": (Y, Y, Y),
    "trip_view": (Y, Y, Y),
    "trip_edit": (Y, Y, Y),  # "Create / edit trips, Stage forms, booking lines": assistant without money fields
    "trip_take": (Y, Y, Y),
    "note_add": (Y, Y, Y),
    "task_update": (Y, Y, Y),
    "members_list": (Y, Y, Y),
    "client_view": (Y, Y, Y),
    "client_edit": (Y, Y, Y),
    "reveal_contact": (Y, Y, Y),  # "Reveal contact ... logged"
    "reveal_sensitive": (Y, Y, Y),  # "Reveal passport / DOB / health / emergency contact"
    "document_upload": (Y, Y, Y),
    "document_download": (Y, Y, Y),
    "proposal_draft": (Y, Y, Y),  # assistant "prepares drafts"
    "reports_view": (Y, Y, Y),  # assistant "without margin"
    "own_mailbox_connect": (Y, Y, Y),  # "own mailbox only"
    "recovery_view": (Y, Y, Y),  # Q1 6.3 banner for every member
    "recovery_cancel": (Y, Y, Y),
    "duty_of_care_view": (Y, Y, Y),  # AA-961 R37 "Travelling now" (A-02): every role; names only through a reveal
    "calendar_view": (Y, Y, Y),  # AA-961 R15: the agency calendar, every role
    "calendar_feed_manage": (Y, Y, Y),  # AA-961: each member's own ICS feed (an owner may revoke any)
    "view_margin": (Y, Y, N),
    "money_fields_edit": (Y, Y, N),
    "aa_confirm": (Y, Y, N),  # AA-947 (S251 decision): confirming an AA hold books with AA; not the assistant
    "proposal_publish": (Y, Y, N),
    "message_send": (Y, Y, ASG),
    "ai_draft_approve": (Y, Y, ASG),
    "payment_mark_received": (Y, Y, N),
    "refund_approve": (Y, THR, N),
    "trip_cancel": (Y, Y, N),
    "trip_delete": (Y, OWN, N),
    "document_delete": (Y, Y, N),
    "campaign_send": (Y, Y, N),
    "workflow_apply": (Y, Y, N),
    "trip_activity_view": (Y, Y, N),  # "Access log: agent sees trip Activity"
    "client_delete": (Y, N, N),
    "client_export": (Y, N, N),
    "agency_export": (Y, N, N),
    "reassign": (Y, N, N),
    "workflow_edit": (Y, N, N),
    "terms_publish": (Y, N, N),
    "support_access_settings": (Y, N, N),
    "team_view": (Y, N, N),
    "team_manage": (Y, N, N),
    "integrations_manage": (Y, N, N),
    "payment_settings": (Y, N, N),
    "plan_view": (Y, N, N),
    "settings_forms_manage": (Y, N, N),
    "access_log_view": (Y, N, N),
    "data_settings": (Y, N, N),  # AA-942 (design §3.5 / §3.6): trash window, auto-archive, legal holds — owner
}
WORD = {Rule.ALLOW: Y, Rule.DENY: N, Rule.OWN: OWN, Rule.ASSIGNED: ASG, Rule.THRESHOLD: THR}


def test_every_action_has_a_design_row_and_every_row_an_action() -> None:
    assert {a.value for a in Action} == set(DESIGN)


@pytest.mark.parametrize("action", list(Action), ids=lambda a: a.value)
@pytest.mark.parametrize("role", ROLES)
def test_every_cell_matches_the_design(role: str, action: Action) -> None:
    assert WORD[rule_for(role, action)] == DESIGN[action.value][ROLES.index(role)]


def test_the_matrix_has_exactly_three_roles_and_every_action_per_role() -> None:
    assert set(MATRIX) == {"owner", "agent", "assistant"}
    for role in ROLES:
        assert set(MATRIX[role]) == set(Action)


def test_the_owner_may_do_everything_and_the_assistant_never_sees_margin() -> None:
    assert all(decide("owner", a).allowed for a in Action)
    assert not may("assistant", Action.VIEW_MARGIN) and may("agent", Action.VIEW_MARGIN)


@pytest.mark.parametrize("role", ["", "admin", "viewer", "OWNER", "editor"])
def test_an_unknown_role_is_denied_everything(role: str) -> None:
    assert not any(decide(role, a).allowed for a in Action)
    assert not any(may(role, a) for a in Action)


def test_own_rule_needs_the_member_s_own_object() -> None:
    assert decide("agent", Action.TRIP_DELETE, own=True).allowed
    assert decide("agent", Action.TRIP_DELETE, own=False).reason == "own_objects_only"
    assert decide("agent", Action.TRIP_DELETE).reason == "own_objects_only"  # missing fact: refused
    assert decide("owner", Action.TRIP_DELETE, own=False).allowed
    assert decide("assistant", Action.TRIP_DELETE, own=True).reason == "role_forbidden"


def test_assigned_rule_for_the_assistant() -> None:
    assert decide("assistant", Action.MESSAGE_SEND, assigned=True).allowed
    assert decide("assistant", Action.MESSAGE_SEND, assigned=False).reason == "assigned_only"
    assert decide("assistant", Action.AI_DRAFT_APPROVE).reason == "assigned_only"
    assert decide("agent", Action.MESSAGE_SEND, assigned=False).allowed


@pytest.mark.parametrize(
    ("amount", "threshold", "allowed"),
    [(999, 1000, True), (1000, 1000, True), (Decimal("1000.01"), 1000, False), (5000, Decimal("1000.00"), False)],
)
def test_threshold_rule_for_an_agent_refund(amount: object, threshold: object, allowed: bool) -> None:
    assert decide("agent", Action.REFUND_APPROVE, amount=amount, threshold=threshold).allowed is allowed  # type: ignore[arg-type]
    assert decide("owner", Action.REFUND_APPROVE, amount=amount, threshold=threshold).allowed  # type: ignore[arg-type]


def test_threshold_rule_without_the_facts_refuses() -> None:
    assert decide("agent", Action.REFUND_APPROVE, amount=10).reason == "above_threshold"
    assert decide("agent", Action.REFUND_APPROVE, threshold=10).reason == "above_threshold"


def test_r_agent_export_and_client_delete_are_403_with_a_denied_audit_event() -> None:
    for action in (Action.CLIENT_EXPORT, Action.AGENCY_EXPORT, Action.CLIENT_DELETE):
        ctx = tenant_ctx(role="agent")
        traveler = str(uuid.uuid4())
        with pytest.raises(Forbidden) as exc:
            require(ctx, action, object_kind="traveler", object_id=traveler)
        assert exc.value.status_code == 403 and exc.value.code == "role_forbidden"
        [event] = ctx.audit_events
        assert (event.action, event.outcome, event.object_kind, event.object_id, event.reason) == (
            action.value,
            "denied",
            "traveler",
            traveler,
            "role_forbidden",
        )


def test_r_agent_refund_above_threshold_is_403() -> None:
    ctx = tenant_ctx(role="agent")
    require(ctx, Action.REFUND_APPROVE, amount=800, threshold=1000)
    assert ctx.audit_events == []
    with pytest.raises(Forbidden) as exc:
        require(ctx, Action.REFUND_APPROVE, object_kind="refund", amount=1200, threshold=1000)
    assert exc.value.status_code == 403 and exc.value.code == "above_threshold"
    assert ctx.audit_events[-1].reason == "above_threshold"
    owner = tenant_ctx(role="owner")
    require(owner, Action.REFUND_APPROVE, amount=1_000_000, threshold=1000)


def test_require_lets_the_owner_through_without_an_event() -> None:
    ctx = tenant_ctx(role="owner")
    for action in Action:
        require(ctx, action, own=True, assigned=True, amount=1, threshold=2)
    assert ctx.audit_events == []
