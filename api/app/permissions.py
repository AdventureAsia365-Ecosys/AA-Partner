"""The CRM permission matrix (design v3 §4.6, card CRM-11 / F4): the single source of what each role may do.

Three roles (``crm.member.role``): ``owner``, ``agent``, ``assistant``. Every agency action has one :class:`Rule`
per role:

=============  ==========================================================================================
``ALLOW``      always
``DENY``       never (403 ``role_forbidden``)
``OWN``        only on the member's own object (an agent deletes their own trips to the trash)
``ASSIGNED``   only on an object assigned to the member (an assistant sends messages on their trips)
``THRESHOLD``  only up to the agency's threshold (an agent approves a refund up to
               ``tenant_settings.refund_owner_threshold_base``)
=============  ==========================================================================================

How it is enforced:

1. :func:`app.tenancy.tenant_tx` takes ``action=`` and refuses the request right after the membership check when the
   role's rule is ``DENY`` (403 ``role_forbidden``, a ``denied`` audit event that survives the rollback). Every
   ``/crm/v1`` route passes its action (``tests/unit/test_code_rules.py`` checks each ``tenant_tx(...)`` call in
   ``app/routers``).
2. Attribute rules (``OWN``, ``ASSIGNED``, ``THRESHOLD``) need the object: the service calls :func:`require` with the
   facts (``own=``, ``assigned=``, ``amount=`` / ``threshold=``). A missing fact refuses (fail closed).
3. Margin-bearing response keys are removed for every role without :data:`Action.VIEW_MARGIN` (the serializer of
   :mod:`app.margin`, on every ``/crm/v1`` route).
4. Membership writes are also guarded by the database (crm-008 ``crm.guard_member_write``): only an owner.

The matrix has every action of design §4.6, including those whose routes arrive with later cards (export, client
delete, refunds, proposals, campaigns, workflows, terms): those cards call :func:`require` with the action listed
here, and ``tests/unit/test_permissions.py`` pins every (role, action) cell against the design table.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from app.errors import Forbidden

if TYPE_CHECKING:
    from app.audit import AuditEvent

ROLES = ("owner", "agent", "assistant")


class Rule(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    OWN = "own"
    ASSIGNED = "assigned"
    THRESHOLD = "threshold"


class Action(StrEnum):
    """Agency actions. The value is the audit code of a refusal (``audit_event.action``)."""

    # Read and work on the agency's data (every role).
    SESSION_VIEW = "session_view"  # who am I in this agency (/me)
    PIPELINE_VIEW = "pipeline_view"
    TRIP_VIEW = "trip_view"
    TRIP_EDIT = "trip_edit"  # create / edit trips, Stage forms, booking lines (no money fields)
    TRIP_TAKE = "trip_take"  # take an unassigned trip
    NOTE_ADD = "note_add"
    TASK_UPDATE = "task_update"
    MEMBERS_LIST = "members_list"  # ids and roles for the assign menu
    CLIENT_VIEW = "client_view"
    CLIENT_EDIT = "client_edit"
    REVEAL_CONTACT = "reveal_contact"
    REVEAL_SENSITIVE = "reveal_sensitive"  # passport / DOB / health / emergency contact (logged)
    DOCUMENT_UPLOAD = "document_upload"
    DOCUMENT_DOWNLOAD = "document_download"
    PROPOSAL_DRAFT = "proposal_draft"
    REPORTS_VIEW = "reports_view"  # the assistant without margin (serializer)
    OWN_MAILBOX_CONNECT = "own_mailbox_connect"
    RECOVERY_VIEW = "recovery_view"  # the owner-recovery banner (Q1 6.3: every member)
    RECOVERY_CANCEL = "recovery_cancel"
    # AA-971 (CRM-13b): whether AA support or break-glass is in the agency now (the banner every member sees, design
    # §2.3 "Support banner"; the grant itself is SUPPORT_ACCESS_SETTINGS, owner only).
    SUPPORT_ACCESS_VIEW = "support_access_view"
    # AA-961 (CRM-30, design R37 / R15): Travelling now (names hidden until a logged reveal), the agency calendar and
    # the member's own ICS feed. Every role: the assistant works the same trips.
    DUTY_OF_CARE_VIEW = "duty_of_care_view"
    CALENDAR_VIEW = "calendar_view"
    CALENDAR_FEED_MANAGE = "calendar_feed_manage"  # own feed only (crm-027 guard); an owner may also revoke others'
    # AA-964 (CRM-33a, M14): read the agency's AI switch and meter, ask for an AI draft (a suggestion only: sending it
    # is MESSAGE_SEND / AI_DRAFT_APPROVE). Every role, like PROPOSAL_DRAFT.
    AI_USE = "ai_use"
    # Money and selling.
    VIEW_MARGIN = "view_margin"  # B2B price ("net"), cost, margin, commission
    MONEY_FIELDS_EDIT = "money_fields_edit"
    AA_CONFIRM = "aa_confirm"  # AA-947: confirm a held AA line, which creates a real AA booking (S251)
    PROPOSAL_PUBLISH = "proposal_publish"
    MESSAGE_SEND = "message_send"
    AI_DRAFT_APPROVE = "ai_draft_approve"
    PAYMENT_MARK_RECEIVED = "payment_mark_received"
    REFUND_APPROVE = "refund_approve"
    TRIP_CANCEL = "trip_cancel"
    TRIP_DELETE = "trip_delete"
    DOCUMENT_DELETE = "document_delete"
    CAMPAIGN_SEND = "campaign_send"
    WORKFLOW_APPLY = "workflow_apply"
    TRIP_ACTIVITY_VIEW = "trip_activity_view"
    # Owner only.
    CLIENT_DELETE = "client_delete"
    CLIENT_EXPORT = "client_export"
    AGENCY_EXPORT = "agency_export"
    REASSIGN = "reassign"  # clients and trips (R8)
    WORKFLOW_EDIT = "workflow_edit"
    TERMS_PUBLISH = "terms_publish"
    SUPPORT_ACCESS_SETTINGS = "support_access_settings"
    TEAM_VIEW = "team_view"
    TEAM_MANAGE = "team_manage"  # invite, change role, remove
    INTEGRATIONS_MANAGE = "integrations_manage"
    PAYMENT_SETTINGS = "payment_settings"
    PLAN_VIEW = "plan_view"
    SETTINGS_FORMS_MANAGE = "settings_forms_manage"  # the public inquiry forms (Settings > Forms)
    ACCESS_LOG_VIEW = "access_log_view"
    DATA_SETTINGS = "data_settings"  # AA-942: trash window, auto-archive, legal holds (Settings > Data and privacy)
    # AA-962 (CRM-31): the agency's own import of its clients, relationships, trips and notes (the agency runs it, R43;
    # a bulk write of client data like the agency export), the merge of two duplicate clients (one goes to the trash,
    # like a client delete) and the onboarding checklist's "Later" / "done" marks.
    CLIENT_IMPORT = "client_import"
    CLIENT_MERGE = "client_merge"
    ONBOARDING_MANAGE = "onboarding_manage"
    # AA-964 (CRM-33a, A-18 AI): switch AI on / off and set the agency's monthly AI budget (owner, like the other
    # Settings sections that bind the agency).
    AI_SETTINGS = "ai_settings"


A, D = Rule.ALLOW, Rule.DENY

# (owner, agent, assistant) per action — design v3 §4.6, final §5.5 (same table).
_TABLE: dict[Action, tuple[Rule, Rule, Rule]] = {
    Action.SESSION_VIEW: (A, A, A),
    Action.PIPELINE_VIEW: (A, A, A),
    Action.TRIP_VIEW: (A, A, A),
    Action.TRIP_EDIT: (A, A, A),
    Action.TRIP_TAKE: (A, A, A),
    Action.NOTE_ADD: (A, A, A),
    Action.TASK_UPDATE: (A, A, A),
    Action.MEMBERS_LIST: (A, A, A),
    Action.CLIENT_VIEW: (A, A, A),
    Action.CLIENT_EDIT: (A, A, A),
    Action.REVEAL_CONTACT: (A, A, A),
    Action.REVEAL_SENSITIVE: (A, A, A),
    Action.DOCUMENT_UPLOAD: (A, A, A),
    Action.DOCUMENT_DOWNLOAD: (A, A, A),
    Action.PROPOSAL_DRAFT: (A, A, A),
    Action.REPORTS_VIEW: (A, A, A),
    Action.OWN_MAILBOX_CONNECT: (A, A, A),
    Action.RECOVERY_VIEW: (A, A, A),
    Action.SUPPORT_ACCESS_VIEW: (A, A, A),
    Action.RECOVERY_CANCEL: (A, A, A),
    Action.DUTY_OF_CARE_VIEW: (A, A, A),
    Action.CALENDAR_VIEW: (A, A, A),
    Action.CALENDAR_FEED_MANAGE: (A, A, A),
    Action.AI_USE: (A, A, A),
    Action.VIEW_MARGIN: (A, A, D),
    Action.MONEY_FIELDS_EDIT: (A, A, D),
    Action.AA_CONFIRM: (A, A, D),
    Action.PROPOSAL_PUBLISH: (A, A, D),
    Action.MESSAGE_SEND: (A, A, Rule.ASSIGNED),
    Action.AI_DRAFT_APPROVE: (A, A, Rule.ASSIGNED),
    Action.PAYMENT_MARK_RECEIVED: (A, A, D),
    Action.REFUND_APPROVE: (A, Rule.THRESHOLD, D),
    Action.TRIP_CANCEL: (A, A, D),
    Action.TRIP_DELETE: (A, Rule.OWN, D),
    Action.DOCUMENT_DELETE: (A, A, D),
    Action.CAMPAIGN_SEND: (A, A, D),
    Action.WORKFLOW_APPLY: (A, A, D),
    Action.TRIP_ACTIVITY_VIEW: (A, A, D),
    Action.CLIENT_DELETE: (A, D, D),
    Action.CLIENT_EXPORT: (A, D, D),
    Action.AGENCY_EXPORT: (A, D, D),
    Action.REASSIGN: (A, D, D),
    Action.WORKFLOW_EDIT: (A, D, D),
    Action.TERMS_PUBLISH: (A, D, D),
    Action.SUPPORT_ACCESS_SETTINGS: (A, D, D),
    Action.TEAM_VIEW: (A, D, D),
    Action.TEAM_MANAGE: (A, D, D),
    Action.INTEGRATIONS_MANAGE: (A, D, D),
    Action.PAYMENT_SETTINGS: (A, D, D),
    Action.PLAN_VIEW: (A, D, D),
    Action.SETTINGS_FORMS_MANAGE: (A, D, D),
    Action.ACCESS_LOG_VIEW: (A, D, D),
    Action.DATA_SETTINGS: (A, D, D),
    Action.CLIENT_IMPORT: (A, D, D),
    Action.CLIENT_MERGE: (A, D, D),
    Action.ONBOARDING_MANAGE: (A, D, D),
    Action.AI_SETTINGS: (A, D, D),
}

MATRIX: dict[str, dict[Action, Rule]] = {
    role: {action: rules[i] for action, rules in _TABLE.items()} for i, role in enumerate(ROLES)
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str  # "allowed" or the refusal code


def rule_for(role: str, action: Action) -> Rule:
    """The role's rule for an action; an unknown role or action is DENY (fail closed)."""
    return MATRIX.get(role, {}).get(action, Rule.DENY)


def may(role: str, action: Action) -> bool:
    """Whether the role could ever be allowed the action (any rule but DENY). Used before the object is known."""
    return rule_for(role, action) is not Rule.DENY


def decide(
    role: str,
    action: Action,
    *,
    own: bool | None = None,
    assigned: bool | None = None,
    amount: Decimal | int | float | None = None,
    threshold: Decimal | int | float | None = None,
) -> Decision:
    """The decision for one (role, action) with the object's facts. A fact the rule needs but is missing refuses."""
    rule = rule_for(role, action)
    if rule is Rule.ALLOW:
        return Decision(True, "allowed")
    if rule is Rule.DENY:
        return Decision(False, "role_forbidden")
    if rule is Rule.OWN:
        return Decision(True, "allowed") if own is True else Decision(False, "own_objects_only")
    if rule is Rule.ASSIGNED:
        return Decision(True, "allowed") if assigned is True else Decision(False, "assigned_only")
    if amount is None or threshold is None:
        return Decision(False, "above_threshold")
    ok = Decimal(str(amount)) <= Decimal(str(threshold))
    return Decision(True, "allowed") if ok else Decision(False, "above_threshold")


class _AuditingContext(Protocol):
    member_role: str | None

    def audit(
        self, action: str, outcome: str, object_kind: str, object_id: str | None = None, **kw: object
    ) -> AuditEvent: ...


def require(
    ctx: _AuditingContext,
    action: Action,
    *,
    object_kind: str = "permission",
    object_id: str | None = None,
    own: bool | None = None,
    assigned: bool | None = None,
    amount: Decimal | int | float | None = None,
    threshold: Decimal | int | float | None = None,
) -> None:
    """Refuse (403) unless the context's member may do ``action`` on this object. A refusal queues a ``denied``
    audit event (``tenant_tx()`` commits it even though the request fails)."""
    decision = decide(ctx.member_role or "", action, own=own, assigned=assigned, amount=amount, threshold=threshold)
    if decision.allowed:
        return
    ctx.audit(action.value, "denied", object_kind, object_id, reason=decision.reason)
    raise Forbidden(decision.reason, f"the {ctx.member_role or 'unknown'} role may not do {action.value}")
