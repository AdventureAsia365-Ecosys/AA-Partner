"""Agency audit events (``crm.audit_event``, crm-003; F5, design v3 §3.3 group J).

Request code queues events on its :class:`~app.tenancy.TenantContext` (``ctx.audit(...)``); ``tenant_tx()`` writes
them when the block ends:

- the block succeeds: every queued event is written in the same transaction;
- the block raises: the route's work is rolled back (savepoint) and only the ``denied`` events are written and
  committed, then the exception goes on. A refused decrypt is therefore on record even when it ends the request.

An event is codes and random ids only — never a value, a name, an e-mail or free text (the table's CHECKs repeat
these patterns). CRM-12 (AA-940) adds the remaining actors, the export and the audit page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ACTOR_KINDS = frozenset({"user", "traveler", "ai_agent", "aa_ops", "aa_staff", "support", "breakglass", "system"})
OUTCOMES = frozenset({"allowed", "denied"})

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

INSERT_SQL = (
    "INSERT INTO crm.audit_event (actor_kind, actor_ref, action, outcome, object_kind, object_id, fields, reason, "
    "request_id, object_ids) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"
)
# AA-937 (crm-006): a list view names the ids it showed (design §4.4), at most this many per event.
MAX_OBJECT_IDS = 200


@dataclass(frozen=True)
class AuditEvent:
    actor_kind: str
    actor_ref: str
    action: str
    outcome: str
    object_kind: str
    object_id: str | None = None
    fields: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None
    request_id: str | None = None
    object_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.actor_kind not in ACTOR_KINDS:
            raise ValueError("unknown actor_kind")
        if self.outcome not in OUTCOMES:
            raise ValueError("outcome must be allowed or denied")
        if not _ID.match(self.actor_ref):
            raise ValueError("actor_ref must be a random id")
        if not _CODE.match(self.action) or not _CODE.match(self.object_kind):
            raise ValueError("action and object_kind are codes")
        if self.object_id is not None and not _ID.match(self.object_id):
            raise ValueError("object_id must be an id")
        if len(self.fields) > 32 or not all(_FIELD.match(f) for f in self.fields):
            raise ValueError("fields are column names")
        if self.reason is not None and not _REASON.match(self.reason):
            raise ValueError("reason is a code, never free text")
        if len(self.object_ids) > MAX_OBJECT_IDS or not all(_ID.match(i) for i in self.object_ids):
            raise ValueError("object_ids are ids")
        if self.request_id is not None and not _REQUEST_ID.match(self.request_id):
            object.__setattr__(self, "request_id", None)

    def row(self) -> tuple:
        return (
            self.actor_kind,
            self.actor_ref,
            self.action,
            self.outcome,
            self.object_kind,
            self.object_id,
            list(self.fields),
            self.reason,
            self.request_id,
            list(self.object_ids),
        )


def current_request_id() -> str | None:
    """The request id bound by ``RequestIdMiddleware`` (structlog contextvars), if any."""
    import structlog

    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None
