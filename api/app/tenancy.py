"""``tenant_tx()``: the only way request code reaches tenant data (design v3 §4.6 "Request context"; ADR 0010 d3).

One transaction on one pooled connection:

1. ``SET LOCAL`` ``app.tenant_id`` (``set_config(..., true)``): from here on every ``crm`` tenant table shows this
   agency's rows only (FORCE RLS policy ``tenant_isolation``, crm-002); before it, none at all;
2. the agency must exist and be ``active`` (a locked agency gets 403 ``tenant_locked``);
3. the membership check: an ``active``, not trashed ``crm.member`` row for the caller's token identity
   ``(iss, sub)`` — never an e-mail lookup (ADR 0006b decision 5). None -> 403 ``not_a_member``;
4. ``SET LOCAL`` ``app.member_role`` (owner / agent / assistant, read by the margin RLS of later cards) and
   ``app.actor`` (``member:<id>``, the ``created_by`` default);
5. optionally ``SET LOCAL ROLE`` to one of the four NOLOGIN readers (``crm_app`` is NOINHERIT and reaches them only
   this way, Infra rl-004). The name comes from :data:`READER_ROLES`, never from input.

The route's work then runs in a savepoint. Audit events it queues with :meth:`TenantContext.audit` are written when
the block ends (AA-934, ``crm.audit_event``):

- normal exit: every queued event is inserted and the transaction commits;
- an exception: the work is rolled back to the savepoint; if ``denied`` events were queued (a refused decrypt, a
  refused access), only those are inserted and the transaction **commits**, then the exception goes on — a denial is
  on record even when it ends the request; with no denial the whole transaction rolls back as before.

Events are inserted as ``crm_app`` (``SET LOCAL ROLE NONE`` first when a reader role was set: the readers have no
privilege on the audit table). Rolling back or committing ends every ``SET LOCAL``. The DB access lint in
``tests/unit/test_code_rules.py`` fails when request code reaches the database any other way than ``ctx.conn`` inside
``async with tenant_tx(...) as ctx``.

Two more entry points, each one transaction with its own narrow context (AA-935):

- :func:`caller_memberships` lists the agencies the caller belongs to (the web needs a tenant id before it can open
  ``tenant_tx()``). It sets ``app.caller_iss`` / ``app.caller_sub`` from the verified token, which the crm-004
  SELECT-only policies ``member_self_lookup`` / ``tenant_member_lookup`` read; it never sets ``app.tenant_id``.
- :func:`provisioning_tx` is the platform context that creates an agency and its first members (the Dev seeder
  ``app.seed`` now, the L1 console ``/crm/ops/v1`` with CRM-13a): ``app.tenant_id`` = the new agency, ``app.actor`` =
  the platform actor, no membership check (there is no member yet). Request code of ``/crm/v1`` never uses it (a unit
  test checks the importers).

Both commit when the block exits normally and roll back on any exception (no audit queue).

Two platform contexts without a member, each with the same audit queue as ``tenant_tx()`` (AA-936):

- :func:`intake_tx` is the public intake form (``/crm/public/v1``, no token): it sets ``app.intake_key`` from the URL
  key, which the crm-005 SELECT-only policy ``intake_form_public_lookup`` reads; the form found decides the agency,
  then ``app.tenant_id`` is set and the agency must be ``active``. Only ``app/intake/public.py`` may open it.
- :func:`system_tx` is a job's context in one agency (``crm_intake_process``): ``app.tenant_id`` from the job payload,
  ``app.actor`` = ``job:<kind>``. Only the job handler modules may open it.
- :func:`platform_tx` (AA-939) is AA staff in one agency (L1 console, ``/crm/ops/v1``): ``app.actor`` =
  ``platform:<staff sub>``. Only ``app/members/recovery.py`` and ``app/ops/console.py`` (AA-941) may open it; it
  takes the agency statuses the call may meet (default active only).

A unit test checks the importers of both (``tests/unit/test_code_rules.py``).

One more platform context (AA-940): :func:`audit_export_tx` is the audit export's read context in one agency
(``app.tenant_id`` only, ``app.actor`` = ``job:crm_audit_export``, no audit queue). Unlike :func:`system_tx` it accepts
a locked or trashed agency: an offboarded agency's audit is still exported and verified until its partitions are
dropped (crm-007 offboarding path). Only the ``app/audit_export/`` job modules may open it.

One more (AA-978): :func:`membership_feed_tx` is one CIS membership event in one agency (actor
``platform:feed-tenant-membership``), opened whatever the agency's status and also for a tenant the CRM does not know
(the event is still recorded). Only ``app/cis/membership_feed.py`` may open it.

One more (AA-942): :func:`lifecycle_tx` is a lifecycle job's context in one agency (the daily ``crm_lifecycle`` run, the
``crm_offboard`` steps): ``app.tenant_id``, ``app.actor`` = ``job:<kind>``, the audit queue of :func:`system_tx`, and
the agency statuses the caller names (a locked agency still has retention; an offboarding one is being erased). Only
the ``app/lifecycle/`` job modules may open it. :func:`as_eraser` switches an open context to ``crm_eraser`` for one
call of the crm-010 deletion functions and back (``SET LOCAL ROLE``; the eraser has no table privilege of its own).

One more (AA-945): :func:`ingest_tx` is the mail ingest's context: the forward-in recipient's local part (an
unauthenticated sender chose it) finds the agency through the crm-015 SELECT-only policy ``ingest_address_lookup``
(``app.ingest_local_part``), then ``app.tenant_id`` and ``app.actor`` = ``job:crm_mail_ingest``. Only
``app/mail/ingest.py`` may open it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import asyncpg

from app.audit import INSERT_SQL, AuditEvent, current_request_id
from app.auth.route_guard import Caller
from app.errors import Conflict, Forbidden, NotFound
from app.margin import current_member_role
from app.observability import reveal_meter
from app.permissions import Action, may

READER_ROLES = frozenset({"crm_ai_reader", "crm_insights", "crm_support_reader", "crm_eraser"})
MEMBER_ROLES = frozenset({"owner", "agent", "assistant"})

TENANT_SQL = "SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL"
MEMBER_SQL = """
SELECT member_id, role FROM crm.member
 WHERE tenant_id = $1 AND cognito_iss = $2 AND cognito_sub = $3 AND status = 'active' AND deleted_at IS NULL
"""


MEMBERSHIPS_SQL = """
SELECT t.tenant_id, t.display_name, t.default_locale, t.status AS tenant_status, m.member_id, m.role
  FROM crm.member m
  JOIN crm.tenant t ON t.tenant_id = m.tenant_id
 WHERE m.cognito_iss = $1 AND m.cognito_sub = $2 AND m.status = 'active' AND m.deleted_at IS NULL
   AND t.deleted_at IS NULL
 ORDER BY t.display_name, t.tenant_id
 LIMIT $3
"""
# A person belongs to a handful of agencies; the cap keeps one response bounded whatever the data says.
MAX_MEMBERSHIPS = 50
PROVISION_ACTOR_PREFIXES = ("platform:", "seed:")
SYSTEM_ACTOR_PREFIXES = ("job:", "seed:")
# AA-939: AA staff in one agency (L1 console). The crm-008 guards let this actor remove the last owner and complete an
# owner recovery, so it has its own context, platform_tx(), which only app/members/recovery.py may open.
PLATFORM_ACTOR_PREFIX = "platform:"
INTAKE_FORM_SQL = """
SELECT f.intake_form_id, f.tenant_id, f.label, f.form_definition_id, f.allowed_origins, f.honeypot_field,
       f.default_owner_member_id, f.lead_source, f.privacy_notice_url
  FROM crm.intake_form f
 WHERE f.public_key = $1 AND f.status = 'active' AND f.deleted_at IS NULL
"""


@dataclass(frozen=True)
class Membership:
    """One agency the caller belongs to (ids, the agency label and role; no personal data)."""

    tenant_id: uuid.UUID
    display_name: str
    default_locale: str
    tenant_status: str
    member_id: uuid.UUID
    member_role: str


@dataclass(frozen=True)
class ProvisioningContext:
    """What platform provisioning may use inside :func:`provisioning_tx`. ``audit`` queues an agency audit event
    (AA-940, design §4.9 "role changes"), written when the block succeeds: actor kind ``aa_ops`` for a ``platform:``
    actor (the L1 console), ``system`` for a ``seed:`` actor."""

    conn: asyncpg.Connection
    tenant_id: uuid.UUID
    actor: str
    audit_events: list[AuditEvent] = field(default_factory=list, repr=False)

    def audit(
        self,
        action: str,
        object_kind: str,
        object_id: str | None = None,
        *,
        fields: tuple[str, ...] = (),
        object_ids: tuple[str, ...] = (),
    ) -> AuditEvent:
        event = AuditEvent(
            actor_kind="aa_ops" if self.actor.startswith("platform:") else "system",
            actor_ref=self.actor,
            action=action,
            outcome="allowed",
            object_kind=object_kind,
            object_id=object_id,
            fields=tuple(fields),
            request_id=current_request_id(),
            object_ids=tuple(object_ids),
        )
        self.audit_events.append(event)
        return event


@dataclass(frozen=True)
class TenantContext:
    """What a route may use inside :func:`tenant_tx`: the connection (inside the transaction) and who is acting."""

    conn: asyncpg.Connection
    tenant_id: uuid.UUID
    member_id: uuid.UUID
    member_role: str
    reader_role: str | None = None
    audit_events: list[AuditEvent] = field(default_factory=list, repr=False)
    # Field encryption (app.vault): DEKs generated in this transaction, never in the process cache until committed.
    dek_memo: dict[tuple[str, int], bytes] = field(default_factory=dict, repr=False)
    dek_current: dict[str, int] = field(default_factory=dict, repr=False)

    def audit(
        self,
        action: str,
        outcome: str,
        object_kind: str,
        object_id: str | None = None,
        *,
        fields: tuple[str, ...] = (),
        reason: str | None = None,
        object_ids: tuple[str, ...] = (),
    ) -> AuditEvent:
        """Queue an ``audit_event`` for this member (written when the ``tenant_tx()`` block ends)."""
        event = AuditEvent(
            actor_kind="user",
            actor_ref=f"member:{self.member_id}",
            action=action,
            outcome=outcome,
            object_kind=object_kind,
            object_id=object_id,
            fields=tuple(fields),
            reason=reason,
            request_id=current_request_id(),
            object_ids=tuple(object_ids),
        )
        self.audit_events.append(event)
        return event


@dataclass(frozen=True)
class SystemContext:
    """A platform context in one agency without a member (:func:`system_tx`, :func:`intake_tx`). Same shape as
    :class:`TenantContext` for :class:`app.vault.Vault` and the key provider (``conn``, ``tenant_id``, the DEK memos,
    ``audit``); ``member_id`` / ``member_role`` are None."""

    conn: asyncpg.Connection
    tenant_id: uuid.UUID
    actor: str
    member_id: None = None
    member_role: None = None
    reader_role: None = None
    audit_events: list[AuditEvent] = field(default_factory=list, repr=False)
    dek_memo: dict[tuple[str, int], bytes] = field(default_factory=dict, repr=False)
    dek_current: dict[str, int] = field(default_factory=dict, repr=False)
    # AA-941: the agency's status when the block opened (system / platform contexts; None for intake / invitations).
    status: str | None = None

    def audit(
        self,
        action: str,
        outcome: str,
        object_kind: str,
        object_id: str | None = None,
        *,
        fields: tuple[str, ...] = (),
        reason: str | None = None,
        object_ids: tuple[str, ...] = (),
    ) -> AuditEvent:
        event = AuditEvent(
            # AA-939: an AA platform action (L1 console: owner recovery, member deactivation) is aa_ops.
            actor_kind=_system_actor_kind(self.actor),
            actor_ref=self.actor,
            action=action,
            outcome=outcome,
            object_kind=object_kind,
            object_id=object_id,
            fields=tuple(fields),
            reason=reason,
            request_id=current_request_id(),
            object_ids=tuple(object_ids),
        )
        self.audit_events.append(event)
        return event


def _system_actor_kind(actor: str) -> str:
    if actor.startswith("platform:"):
        return "aa_ops"
    if actor.startswith("invitation:"):
        return "user"  # the invited person accepting (AA-939)
    if actor.startswith("client_link:"):
        return "traveler"  # a client filling a form from a signed link (AA-943)
    return "system"


@dataclass(frozen=True)
class ExportContext:
    """The audit export's context in one agency (:func:`audit_export_tx`): the connection and the agency only."""

    conn: asyncpg.Connection
    tenant_id: uuid.UUID
    actor: str = "job:crm_audit_export"


@dataclass(frozen=True)
class LifecycleContext:
    """A lifecycle job's context in one agency (:func:`lifecycle_tx`). Same shape as :class:`SystemContext` for
    :class:`app.vault.Vault` and the key provider; ``status`` is the agency's status when the block opened."""

    conn: asyncpg.Connection
    tenant_id: uuid.UUID
    actor: str
    status: str
    member_id: None = None
    member_role: None = None
    reader_role: None = None
    audit_events: list[AuditEvent] = field(default_factory=list, repr=False)
    dek_memo: dict[tuple[str, int], bytes] = field(default_factory=dict, repr=False)
    dek_current: dict[str, int] = field(default_factory=dict, repr=False)

    def audit(
        self,
        action: str,
        outcome: str,
        object_kind: str,
        object_id: str | None = None,
        *,
        fields: tuple[str, ...] = (),
        reason: str | None = None,
        object_ids: tuple[str, ...] = (),
    ) -> AuditEvent:
        event = AuditEvent(
            actor_kind="system",
            actor_ref=self.actor,
            action=action,
            outcome=outcome,
            object_kind=object_kind,
            object_id=object_id,
            fields=tuple(fields),
            reason=reason,
            request_id=current_request_id(),
            object_ids=tuple(object_ids),
        )
        self.audit_events.append(event)
        return event


@dataclass(frozen=True)
class IntakeForm:
    """The public form :func:`intake_tx` found (ids, codes and the agency's own settings; no personal data)."""

    intake_form_id: uuid.UUID
    tenant_id: uuid.UUID
    label: str
    form_definition_id: uuid.UUID
    allowed_origins: tuple[str, ...]
    honeypot_field: str
    default_owner_member_id: uuid.UUID | None
    lead_source: str
    privacy_notice_url: str | None


@asynccontextmanager
async def tenant_tx(
    pool: asyncpg.Pool,
    caller: Caller,
    tenant_id: uuid.UUID,
    *,
    reader_role: str | None = None,
    action: Action | None = None,
) -> AsyncIterator[TenantContext]:
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    if reader_role is not None and reader_role not in READER_ROLES:
        raise ValueError(f"not a CRM reader role: {reader_role!r}")
    pending: Exception | None = None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        status = await conn.fetchval(TENANT_SQL, tenant_id)
        if status is None:
            # Unknown, trashed or another agency's id: the same answer, so ids cannot be probed.
            raise Forbidden("not_a_member")
        if status != "active":
            raise Forbidden("tenant_locked")
        member = await conn.fetchrow(MEMBER_SQL, tenant_id, caller.iss, caller.sub)
        if member is None:
            raise Forbidden("not_a_member")
        role = member["role"]
        if role not in MEMBER_ROLES:  # the CHECK makes this impossible; refuse rather than trust it
            raise Forbidden("not_a_member")
        await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        await conn.execute("SELECT set_config('app.actor', $1, true)", f"member:{member['member_id']}")
        if reader_role is not None:
            # A role name cannot be a bind parameter; READER_ROLES is a fixed allow-list checked above.
            await conn.execute(f"SET LOCAL ROLE {reader_role}")
        ctx = TenantContext(
            conn=conn, tenant_id=tenant_id, member_id=member["member_id"], member_role=role, reader_role=reader_role
        )
        # AA-939: the role the response serializer filters for (app.margin), in this request's context.
        current_member_role.set(role)
        try:
            if action is not None and not may(role, action):
                # The permission matrix (app.permissions): refused before any work, the denial is on record.
                ctx.audit(action.value, "denied", "permission", reason="role_forbidden")
                raise Forbidden("role_forbidden", f"the {role} role may not do {action.value}")
            async with conn.transaction():  # savepoint: the route's work
                yield ctx
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == LEGAL_HOLD_SQLSTATE:
                # crm-010 hold rule 4h: the database refused to delete, trash or unlink a held row, whatever route
                # tried (remove from a party, delete a document, remove a relation...): 409 legal_hold, on record.
                ctx.audit("change", "denied", "legal_hold", reason="legal_hold")
                exc = Conflict("legal_hold", "an active legal hold covers this record; it cannot be removed")
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc  # commit the denials, then let the exception go on
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending
    # Committed: count what was really released (design §4.4 reveals per member per hour, app.observability).
    released = sum(1 for e in ctx.audit_events if e.action == "reveal" and e.outcome == "allowed")
    if released:
        reveal_meter.METER.record(ctx.tenant_id, ctx.member_id, released)


@asynccontextmanager
async def system_tx(pool: asyncpg.Pool, tenant_id: uuid.UUID, *, actor: str) -> AsyncIterator[SystemContext]:
    """A job's context in one agency (no member). The agency must exist and be active (else 403 ``tenant_locked`` /
    ``not_a_member``, which a job handler turns into a permanent failure). Audit semantics as :func:`tenant_tx`.
    Actors ``job:`` / ``seed:`` only: an AA staff action uses :func:`platform_tx`."""
    if not actor.startswith(SYSTEM_ACTOR_PREFIXES) or len(actor) > 80:
        raise ValueError(f"system actor must start with one of {SYSTEM_ACTOR_PREFIXES}")
    async with _agency_tx(pool, tenant_id, actor) as ctx:
        yield ctx


# AA-941: the agency statuses an L1 console call may meet. Default active only (every write that a member must be able
# to answer: owner recovery, invitations, data requests); reads name every status; lock / unlock active + locked.
PLATFORM_DEFAULT_STATUSES = frozenset({"active"})
TENANT_STATUSES = frozenset({"active", "locked", "offboarding", "offboarded"})


@asynccontextmanager
async def platform_tx(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    *,
    actor: str,
    statuses: frozenset[str] = PLATFORM_DEFAULT_STATUSES,
) -> AsyncIterator[SystemContext]:
    """AA staff acting in one agency from the L1 console (``/crm/ops/v1``, AA-939 / AA-941): ``app.actor`` =
    ``platform:<staff sub>`` (audit actor kind ``aa_ops``). The crm-008 guards let this actor do what no member may
    (remove the last owner, open and complete an owner recovery, write entitlements), so only
    ``app/members/recovery.py`` and ``app/ops/console.py`` open it, and only the ops router calls their platform
    functions (``test_code_rules``). Audit semantics as :func:`system_tx`.

    The agency must exist (else **404** ``tenant_not_found``: the L1 console knows the agency list, so there is nothing
    to hide) and be in ``statuses`` (default active only; else **409** ``tenant_locked`` for a locked agency,
    ``tenant_status`` otherwise). ``ctx.status`` is the status the block opened with."""
    if not actor.startswith(PLATFORM_ACTOR_PREFIX) or len(actor) > 80:
        raise ValueError(f"platform actor must start with {PLATFORM_ACTOR_PREFIX!r}")
    if not statuses or not statuses <= TENANT_STATUSES:
        raise ValueError(f"statuses must be a non-empty subset of {sorted(TENANT_STATUSES)}")
    async with _agency_tx(pool, tenant_id, actor, statuses=statuses) as ctx:
        yield ctx


# AA-978: the CIS membership feed's actor. crm-013 lets only this actor name a cis_* lock source
# (crm.tenant.lock_source); a platform: actor, so the crm-008 guards let it disable a member, the last owner included.
MEMBERSHIP_FEED_ACTOR = "platform:feed-tenant-membership"


@asynccontextmanager
async def membership_feed_tx(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> AsyncIterator[SystemContext]:
    """One CIS ``tenant.membership.v1`` event in one agency (AA-978, ``crm_membership_pull``): ``app.tenant_id`` = the
    event's tenant, ``app.actor`` = :data:`MEMBERSHIP_FEED_ACTOR` (audit actor kind ``aa_ops``: CIS is AA's directory).

    Unlike :func:`platform_tx` it refuses no agency: CIS reports every CIS tenant, most of which have no CRM agency, and
    the event must still be recorded in ``crm.membership_inbox`` (FORCE RLS on ``app.tenant_id``, no foreign key).
    ``ctx.status`` is the agency's status, ``None`` when the CRM has no (untrashed) agency with that id; the caller
    decides what each status allows. The work runs in a savepoint; queued audit events are written on success, only
    when the agency exists (``crm.audit_event`` references it). Any exception rolls the whole event back. Only
    ``app/cis/membership_feed.py`` may open it (``tests/unit/test_code_rules.py``)."""
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        status = await conn.fetchval(TENANT_SQL, tenant_id)
        await conn.execute("SELECT set_config('app.actor', $1, true)", MEMBERSHIP_FEED_ACTOR)
        ctx = SystemContext(conn=conn, tenant_id=tenant_id, actor=MEMBERSHIP_FEED_ACTOR, status=status)
        async with conn.transaction():  # savepoint: the event's work
            yield ctx
        if status is not None:
            await _write_audit(ctx, ctx.audit_events)


@asynccontextmanager
async def _agency_tx(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, actor: str, *, statuses: frozenset[str] | None = None
) -> AsyncIterator[SystemContext]:
    """``statuses`` None = a job's context (403 ``not_a_member`` / ``tenant_locked``, unchanged since AA-936); a set =
    the platform context (404 ``tenant_not_found``, 409 ``tenant_locked`` / ``tenant_status``)."""
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    pending: Exception | None = None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        status = await conn.fetchval(TENANT_SQL, tenant_id)
        if statuses is None:
            if status is None:
                raise Forbidden("not_a_member")
            if status != "active":
                raise Forbidden("tenant_locked")
        else:
            if status is None:
                raise NotFound("tenant_not_found", "no agency with this id")
            if status not in statuses:
                if status == "locked":
                    raise Conflict("tenant_locked", "the agency is locked; unlock it first")
                raise Conflict("tenant_status", f"the agency is {status}", state=status)
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        ctx = SystemContext(conn=conn, tenant_id=tenant_id, actor=actor, status=status)
        try:
            async with conn.transaction():  # savepoint: the job's work
                yield ctx
        except Exception as exc:
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending


@asynccontextmanager
async def intake_tx(pool: asyncpg.Pool, public_key: str) -> AsyncIterator[tuple[SystemContext, IntakeForm]]:
    """The public intake form's context: the form found by its URL key decides the agency (no token, no member).
    An unknown, disabled or trashed key, and a form of a locked or trashed agency, all raise 404 ``form_not_found``
    (the same answer, so keys cannot be probed). ``app.actor`` = ``intake_form:<id>``."""
    if not isinstance(public_key, str) or not 20 <= len(public_key) <= 64:
        raise NotFound("form_not_found")
    pending: Exception | None = None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.intake_key', $1, true)", public_key)
        row = await conn.fetchrow(INTAKE_FORM_SQL, public_key)
        await conn.execute("SELECT set_config('app.intake_key', '', true)")
        if row is None:
            raise NotFound("form_not_found")
        tenant_id = row["tenant_id"]
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        if await conn.fetchval(TENANT_SQL, tenant_id) != "active":
            raise NotFound("form_not_found")
        actor = f"intake_form:{row['intake_form_id']}"
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        form = IntakeForm(
            intake_form_id=row["intake_form_id"],
            tenant_id=tenant_id,
            label=row["label"],
            form_definition_id=row["form_definition_id"],
            allowed_origins=tuple(row["allowed_origins"] or ()),
            honeypot_field=row["honeypot_field"],
            default_owner_member_id=row["default_owner_member_id"],
            lead_source=row["lead_source"],
            privacy_notice_url=row["privacy_notice_url"],
        )
        ctx = SystemContext(conn=conn, tenant_id=tenant_id, actor=actor)
        try:
            async with conn.transaction():  # savepoint: the route's work
                yield ctx, form
        except Exception as exc:
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending


EXPORT_ACTOR = "job:crm_audit_export"
RETENTION_ACTOR = "job:crm_audit_retention"
EXPORT_ACTORS = frozenset({EXPORT_ACTOR, RETENTION_ACTOR})
TENANT_EXISTS_SQL = "SELECT 1 FROM crm.tenant WHERE tenant_id = $1"


@asynccontextmanager
async def audit_export_tx(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, actor: str = EXPORT_ACTOR
) -> AsyncIterator[ExportContext]:
    """One transaction in one agency for the audit export and its verification (AA-940). The agency must exist; a
    locked or trashed one is accepted (its audit is exported until its partitions go). Commits on a normal exit.
    ``actor``: ``job:crm_audit_export`` or ``job:crm_audit_retention`` (the daily run's per-agency event)."""
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    if actor not in EXPORT_ACTORS:
        raise ValueError(f"audit export actor must be one of {sorted(EXPORT_ACTORS)}")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        if await conn.fetchval(TENANT_EXISTS_SQL, tenant_id) is None:
            raise NotFound("tenant_not_found")
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        yield ExportContext(conn=conn, tenant_id=tenant_id, actor=actor)


INVITATION_SQL = """
SELECT invitation_id, tenant_id, role, status, expires_at, cognito_iss, cognito_sub, member_id, email_enc,
       expires_at <= now() AS expired
  FROM crm.member_invitation
 WHERE token_hash = decode($1, 'hex') AND deleted_at IS NULL
"""
# Once the agency is set, the same row again, locked: two claims (or two accepts) of one invitation run one after the
# other, and the second sees what the first committed (READ COMMITTED re-reads a row after waiting for its lock).
# The lookup policy is SELECT-only, so the lock goes through tenant_isolation (the UPDATE policy) here.
INVITATION_LOCK_SQL = """
SELECT invitation_id, tenant_id, role, status, expires_at, cognito_iss, cognito_sub, member_id, email_enc,
       expires_at <= now() AS expired
  FROM crm.member_invitation
 WHERE invitation_id = $1 AND deleted_at IS NULL
   FOR UPDATE
"""


@dataclass(frozen=True)
class Invitation:
    """The invitation :func:`invitation_tx` found by its token (ids, codes, the sealed e-mail; nothing decrypted)."""

    invitation_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    status: str
    expired: bool
    cognito_iss: str | None
    cognito_sub: str | None
    member_id: uuid.UUID | None
    email_enc: bytes


@asynccontextmanager
async def invitation_tx(pool: asyncpg.Pool, token_hash_hex: str) -> AsyncIterator[tuple[SystemContext, Invitation]]:
    """The invitation claim / accept context (AA-939, root ADR 0006b decision 5): the token's SHA-256 (hex) finds one
    ``crm.member_invitation`` row through the crm-008 SELECT-only policy ``invitation_token_lookup`` before any agency
    is known; then ``app.tenant_id`` = its agency (which must be active) and ``app.actor`` = ``invitation:<id>`` (the
    crm-008 member guard lets that actor write exactly the member the invitation names). Unknown or trashed token, or
    a locked agency: 404 ``invitation_not_found`` (tokens cannot be probed). Audit semantics as :func:`tenant_tx`.
    Only ``app/members/invitations.py`` may open it."""
    if not isinstance(token_hash_hex, str) or len(token_hash_hex) != 64:
        raise NotFound("invitation_not_found")
    pending: Exception | None = None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.invite_token_hash', $1, true)", token_hash_hex)
        row = await conn.fetchrow(INVITATION_SQL, token_hash_hex)
        await conn.execute("SELECT set_config('app.invite_token_hash', '', true)")
        if row is None:
            raise NotFound("invitation_not_found")
        tenant_id = row["tenant_id"]
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        if await conn.fetchval(TENANT_SQL, tenant_id) != "active":
            raise NotFound("invitation_not_found")
        row = await conn.fetchrow(INVITATION_LOCK_SQL, row["invitation_id"])
        if row is None:
            raise NotFound("invitation_not_found")
        actor = f"invitation:{row['invitation_id']}"
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        invitation = Invitation(
            invitation_id=row["invitation_id"],
            tenant_id=tenant_id,
            role=row["role"],
            status=row["status"],
            expired=bool(row["expired"]),
            cognito_iss=row["cognito_iss"],
            cognito_sub=row["cognito_sub"],
            member_id=row["member_id"],
            email_enc=bytes(row["email_enc"]),
        )
        ctx = SystemContext(conn=conn, tenant_id=tenant_id, actor=actor)
        try:
            async with conn.transaction():  # savepoint: the step's work
                yield ctx, invitation
        except Exception as exc:
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending


# ── AA-979 P2b: Content studio invitations (crm-020, platform table crm.studio_invitation) ────────────────────────────
STUDIO_INVITATION_BY_LOOKUP_SQL = """
SELECT studio_invitation_id, cis_tenant_id, cis_invitation_id, email_enc, status, locale, expires_at,
       expires_at <= now() AS expired, cognito_iss, cognito_sub, activation_attempts
  FROM crm.studio_invitation WHERE token_hash = decode($1, 'hex')
   FOR UPDATE
"""
STUDIO_ACTIVATION_ACTOR = "job:crm_studio_activation"


@dataclass(frozen=True)
class StudioContext:
    """A Content studio invitation context (AA-979): no agency (the CIS tenant may have none), so no ``tenant_id``,
    no vault and no CRM audit (``crm.audit_event`` belongs to an agency); the code logs ids and codes instead."""

    conn: asyncpg.Connection
    actor: str


@dataclass(frozen=True)
class StudioInvitation:
    """The row :func:`studio_invitation_tx` found by its token (ids, codes and the token-sealed e-mail)."""

    studio_invitation_id: uuid.UUID
    cis_tenant_id: uuid.UUID
    cis_invitation_id: uuid.UUID
    email_enc: bytes
    status: str
    locale: str
    expired: bool
    cognito_iss: str | None
    cognito_sub: str | None


@asynccontextmanager
async def studio_platform_tx(pool: asyncpg.Pool, *, actor: str) -> AsyncIterator[StudioContext]:
    """AA staff (``platform:<staff sub>``, the L1 relay of a CIS invitation) or the activation job
    (:data:`STUDIO_ACTIVATION_ACTOR`) on ``crm.studio_invitation``: the crm-020 row security lets exactly these actors
    reach the rows, and its guard (CRM37) decides what each may change. One transaction. Only
    ``app/members/studio.py`` may open it (``tests/unit/test_code_rules.py``)."""
    if not (actor.startswith(PLATFORM_ACTOR_PREFIX) or actor == STUDIO_ACTIVATION_ACTOR) or len(actor) > 100:
        raise ValueError("studio actor must be platform:<sub> or the activation job")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        yield StudioContext(conn=conn, actor=actor)


@asynccontextmanager
async def studio_invitation_tx(
    pool: asyncpg.Pool, token_hash_hex: str
) -> AsyncIterator[tuple[StudioContext, StudioInvitation]]:
    """The studio claim / accept context (AA-979): the token's SHA-256 (hex) finds and locks one
    ``crm.studio_invitation`` row through the crm-020 token policies, ``app.actor`` = ``invitation:<id>`` (the token
    holder). Unknown token: 404 ``invitation_not_found``. Only ``app/members/studio.py`` may open it."""
    if not isinstance(token_hash_hex, str) or len(token_hash_hex) != 64:
        raise NotFound("invitation_not_found")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.invite_token_hash', $1, true)", token_hash_hex)
        row = await conn.fetchrow(STUDIO_INVITATION_BY_LOOKUP_SQL, token_hash_hex)
        if row is None:
            raise NotFound("invitation_not_found")
        actor = f"invitation:{row['studio_invitation_id']}"
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        invitation = StudioInvitation(
            studio_invitation_id=row["studio_invitation_id"],
            cis_tenant_id=row["cis_tenant_id"],
            cis_invitation_id=row["cis_invitation_id"],
            email_enc=bytes(row["email_enc"]),
            status=row["status"],
            locale=row["locale"],
            expired=bool(row["expired"]),
            cognito_iss=row["cognito_iss"],
            cognito_sub=row["cognito_sub"],
        )
        yield StudioContext(conn=conn, actor=actor), invitation


CLIENT_LINK_SQL = """
SELECT link_id, tenant_id FROM crm.client_link
 WHERE token_hash = decode($1, 'hex') AND status IN ('sent', 'opened', 'submitted') AND expires_at > now()
   AND deleted_at IS NULL
"""
# The same row again inside the agency, locked (two submissions through one link run one after the other) and still
# live: a link revoked or expired between the two reads is gone too.
CLIENT_LINK_LOCK_SQL = """
SELECT l.link_id, l.trip_id, l.traveler_id, l.purpose, l.status, l.expires_at
  FROM crm.client_link l JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
 WHERE l.link_id = $1 AND l.status IN ('sent', 'opened', 'submitted') AND l.expires_at > now()
   AND l.deleted_at IS NULL
   FOR UPDATE OF l
"""


@dataclass(frozen=True)
class ClientLink:
    """The live signed link :func:`client_link_tx` found by its token (ids and codes only)."""

    link_id: uuid.UUID
    tenant_id: uuid.UUID
    trip_id: uuid.UUID
    traveler_id: uuid.UUID | None
    purpose: str
    status: str


@asynccontextmanager
async def client_link_tx(
    pool: asyncpg.Pool, token_hash_hex: str, *, purpose: str
) -> AsyncIterator[tuple[SystemContext, ClientLink]]:
    """A client page opened from a signed link (AA-943, crm-012; the Stage 2 planning form C-02): the token's SHA-256
    (hex) finds one live ``crm.client_link`` row through the SELECT-only policy ``client_link_token_lookup`` before
    any agency is known; then ``app.tenant_id`` = its agency (which must be active) and ``app.actor`` =
    ``client_link:<id>``, and the row is re-read locked. Unknown, expired, revoked or trashed token, a link made for
    another ``purpose`` (a page opens only its own kind of link), a trashed trip or a locked agency: 404
    ``link_not_found`` (the same answer, so tokens cannot be probed). Audit semantics as
    :func:`system_tx`. Only ``app/intake/stage2_public.py`` may open it."""
    if not isinstance(token_hash_hex, str) or len(token_hash_hex) != 64:
        raise NotFound("link_not_found")
    pending: Exception | None = None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.client_link_hash', $1, true)", token_hash_hex)
        row = await conn.fetchrow(CLIENT_LINK_SQL, token_hash_hex)
        await conn.execute("SELECT set_config('app.client_link_hash', '', true)")
        if row is None:
            raise NotFound("link_not_found")
        tenant_id = row["tenant_id"]
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        if await conn.fetchval(TENANT_SQL, tenant_id) != "active":
            raise NotFound("link_not_found")
        row = await conn.fetchrow(CLIENT_LINK_LOCK_SQL, row["link_id"])
        if row is None or row["purpose"] != purpose:
            raise NotFound("link_not_found")
        actor = f"client_link:{row['link_id']}"
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        link = ClientLink(
            link_id=row["link_id"],
            tenant_id=tenant_id,
            trip_id=row["trip_id"],
            traveler_id=row["traveler_id"],
            purpose=row["purpose"],
            status=row["status"],
        )
        ctx = SystemContext(conn=conn, tenant_id=tenant_id, actor=actor)
        try:
            async with conn.transaction():  # savepoint: the page's work
                yield ctx, link
        except Exception as exc:
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending


INGEST_ADDRESS_SQL = """
SELECT ingest_address_id, tenant_id FROM crm.ingest_address
 WHERE local_part = $1 AND active AND deleted_at IS NULL
"""
INGEST_ACTOR = "job:crm_mail_ingest"
_LOCAL_PART = re.compile(r"^[a-z0-9][a-z0-9-]{6,62}[a-z0-9]$")


@dataclass(frozen=True)
class IngestAddress:
    """The active forward-in address :func:`ingest_tx` found by its local part (ids only)."""

    ingest_address_id: uuid.UUID
    tenant_id: uuid.UUID


@asynccontextmanager
async def ingest_tx(pool: asyncpg.Pool, local_part: str) -> AsyncIterator[tuple[SystemContext, IngestAddress]]:
    """The mail ingest's context (AA-945, crm-015): the recipient's local part finds one active
    ``crm.ingest_address`` through the SELECT-only policy ``ingest_address_lookup`` before any agency is known (an
    unauthenticated sender chose the address); then ``app.tenant_id`` = its agency (which must be active) and
    ``app.actor`` = ``job:crm_mail_ingest``. An unknown, inactive or trashed address, or an agency that is not active:
    404 ``address_not_found`` (the caller discards the mail, card R). Audit semantics as :func:`system_tx`. Only
    ``app/mail/ingest.py`` may open it."""
    if not isinstance(local_part, str) or not _LOCAL_PART.match(local_part):
        raise NotFound("address_not_found")
    pending: Exception | None = None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.ingest_local_part', $1, true)", local_part)
        row = await conn.fetchrow(INGEST_ADDRESS_SQL, local_part)
        await conn.execute("SELECT set_config('app.ingest_local_part', '', true)")
        if row is None:
            raise NotFound("address_not_found")
        tenant_id = row["tenant_id"]
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        if await conn.fetchval(TENANT_SQL, tenant_id) != "active":
            raise NotFound("address_not_found")
        await conn.execute("SELECT set_config('app.actor', $1, true)", INGEST_ACTOR)
        address = IngestAddress(ingest_address_id=row["ingest_address_id"], tenant_id=tenant_id)
        ctx = SystemContext(conn=conn, tenant_id=tenant_id, actor=INGEST_ACTOR, status="active")
        try:
            async with conn.transaction():  # savepoint: the mail's work
                yield ctx, address
        except Exception as exc:
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending


# crm-010: SQLSTATE of the hold rule (erase_subject, the hold guard triggers).
LEGAL_HOLD_SQLSTATE = "CRM10"
LIFECYCLE_ACTORS = frozenset({"job:crm_lifecycle", "job:crm_offboard"})
TENANT_STATUS_SQL = "SELECT status FROM crm.tenant WHERE tenant_id = $1"
ERASER_ROLE = "crm_eraser"


@asynccontextmanager
async def lifecycle_tx(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, actor: str, statuses: frozenset[str] = frozenset({"active", "locked"})
) -> AsyncIterator[LifecycleContext]:
    """One transaction in one agency for a lifecycle job (AA-942). The agency must exist and be in ``statuses``
    (else 403 ``tenant_status``). Audit semantics as :func:`system_tx`."""
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    if actor not in LIFECYCLE_ACTORS:
        raise ValueError(f"lifecycle actor must be one of {sorted(LIFECYCLE_ACTORS)}")
    pending: Exception | None = None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        status = await conn.fetchval(TENANT_STATUS_SQL, tenant_id)
        if status is None:
            raise NotFound("tenant_not_found")
        if status not in statuses:
            raise Forbidden("tenant_status", f"the agency is {status}")
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        ctx = LifecycleContext(conn=conn, tenant_id=tenant_id, actor=actor, status=status)
        try:
            async with conn.transaction():  # savepoint: the job's work
                yield ctx
        except Exception as exc:
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending


@asynccontextmanager
async def as_eraser(ctx: TenantContext | LifecycleContext) -> AsyncIterator[asyncpg.Connection]:
    """Run the block as ``crm_eraser`` (crm-010: the only role that executes ``crm.erase_subject`` and
    ``crm.lifecycle_purge_expired``), then back to the login role. Only for a context opened without a reader role."""
    if ctx.reader_role is not None:
        raise ValueError("as_eraser needs a context without a reader role")
    # A savepoint: when the call fails, rolling it back also undoes the SET LOCAL ROLE (and the function's own
    # app.erasing flag), so the context goes on as the login role.
    async with ctx.conn.transaction():
        await ctx.conn.execute(f"SET LOCAL ROLE {ERASER_ROLE}")
        yield ctx.conn
        await ctx.conn.execute("SET LOCAL ROLE NONE")


async def _write_audit(ctx: TenantContext | SystemContext | LifecycleContext, events: list[AuditEvent]) -> None:
    if not events:
        return
    if ctx.reader_role is not None:
        await ctx.conn.execute("SET LOCAL ROLE NONE")
    await ctx.conn.executemany(INSERT_SQL, [e.row() for e in events])


async def caller_memberships(pool: asyncpg.Pool, caller: Caller) -> list[Membership]:
    """The caller's active memberships in agencies that are not trashed (locked ones included, with their status)."""
    if not caller.iss or not caller.sub:
        raise Forbidden("not_a_member")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.caller_iss', $1, true)", caller.iss)
        await conn.execute("SELECT set_config('app.caller_sub', $1, true)", caller.sub)
        rows = await conn.fetch(MEMBERSHIPS_SQL, caller.iss, caller.sub, MAX_MEMBERSHIPS)
    return [
        Membership(
            tenant_id=r["tenant_id"],
            display_name=r["display_name"],
            default_locale=r["default_locale"],
            tenant_status=r["tenant_status"],
            member_id=r["member_id"],
            member_role=r["role"],
        )
        for r in rows
        if r["role"] in MEMBER_ROLES
    ]


@asynccontextmanager
async def provisioning_tx(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, actor: str
) -> AsyncIterator[ProvisioningContext]:
    """Platform context for creating one agency and its first members (no membership check: none exists yet)."""
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    if not actor.startswith(PROVISION_ACTOR_PREFIXES) or len(actor) > 120:
        raise ValueError(f"provisioning actor must start with one of {PROVISION_ACTOR_PREFIXES}")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        ctx = ProvisioningContext(conn=conn, tenant_id=tenant_id, actor=actor)
        yield ctx
        # The block succeeded: its audit events go in the same transaction (a failed write fails the provisioning).
        if ctx.audit_events:
            await conn.executemany(INSERT_SQL, [e.row() for e in ctx.audit_events])


# What helpers that work inside any of the contexts accept (the DB lint allows ``<param>.conn`` on this annotation).
AnyContext = TenantContext | SystemContext | LifecycleContext
