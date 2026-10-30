"""What a portability export holds (AA-967, design §4.10 "full CSV + JSON"): every tenant table of schema ``crm`` the
login role can read, found in the catalog at build time (a table a later card adds is exported without a code change),
minus the few that are never handed out; per row every column, minus the index / secret / token columns; every
``*_enc`` column opened under its DEK purpose and exported without the suffix.

Never exported: the key rows and the agency's vendor secrets (``tenant_key``, ``integration_credential``), the F3
placeholder maps and the portal codes (short-lived secrets: ``placeholder_map``, ``otp_challenge``), and the audit with
its export chain (the agency downloads it at Settings > Access log, AA-940).

A client export (scope ``traveler``) takes the rows about that client: at least every row the traveller erase of
``crm.erase_subject`` deletes or changes (the same WHERE clauses, :data:`CLIENT_WHERE`; a unit test compares the two
lists, so a table added to the erase cannot be missed here), plus what the erase keeps (the signed evidence, crm-025)
and the trips they are on: the traveller row, the F2 profile and its history and party rows (by the profile token),
every row that names the traveller (``traveler_id``), both ends of a relation, the signatures they gave as a guardian,
the name they typed to accept a proposal (only the acceptance columns of that proposal, :data:`CLIENT_COLUMNS`), the
call slots their form submissions booked, the attempts of their dispatch rows and the messages of their conversations.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import uuid
from dataclasses import dataclass
from typing import Any

NEVER_EXPORT_TABLES = frozenset(
    {
        "tenant_key",
        "integration_credential",
        "placeholder_map",
        "otp_challenge",
        "audit_event",
        "audit_export_manifest",
        "audit_export_tenant",
    }
)
# Hashes of link / invitation tokens, the F2 pointer (the profile rows are exported themselves), the KEK material.
NEVER_EXPORT_COLUMNS = frozenset(
    {"profile_token_enc", "token_hash", "link_sha256", "code_enc", "secret_enc", "map_enc", "wrapped_dek"}
)
# The DEK purpose of each encrypted column: the vault DEK unless named here (mail content: app.mail, CRM-17; the
# sealed signing envelope: crm-025, the docs DEK).
MAIL_TABLES = frozenset({"message", "channel_account", "saved_reply", "message_template", "ingest_address"})
COLUMN_PURPOSES: dict[tuple[str, str], str] = {("signature_event", "envelope_enc"): "docs"}

TABLES_SQL = """
SELECT c.relname::text AS name,
       ARRAY(SELECT a.attname::text FROM pg_catalog.pg_index i
               JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
              WHERE i.indrelid = c.oid AND i.indisprimary ORDER BY a.attnum) AS pk,
       ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
              WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum) AS columns,
       ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
              WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                AND a.atttypid IN ('json'::regtype, 'jsonb'::regtype) ORDER BY a.attnum) AS json_columns
  FROM pg_catalog.pg_class c
 WHERE c.relnamespace = 'crm'::regnamespace AND c.relkind IN ('r', 'p') AND NOT c.relispartition
   AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute t
                WHERE t.attrelid = c.oid AND t.attname = 'tenant_id' AND NOT t.attisdropped)
   AND pg_catalog.has_table_privilege(c.oid, 'SELECT')
 ORDER BY 1
"""


@dataclass(frozen=True)
class Table:
    name: str
    pk: tuple[str, ...]
    columns: tuple[str, ...]
    json_columns: frozenset[str] = frozenset()

    @property
    def row_key(self) -> str:
        """The column whose value is the AAD row id of the table's ``*_enc`` columns (the single primary key)."""
        return self.pk[0] if len(self.pk) == 1 else ""

    def exported_columns(self) -> list[str]:
        return [c for c in self.columns if not (c.endswith("_bidx") or c in NEVER_EXPORT_COLUMNS)]

    def out_name(self, column: str) -> str:
        return column[: -len("_enc")] if column.endswith("_enc") else column

    def purpose(self, column: str) -> str:
        if (self.name, column) in COLUMN_PURPOSES:
            return COLUMN_PURPOSES[(self.name, column)]
        return "mail" if self.name in MAIL_TABLES else "vault"


def exported(tables: list[Table]) -> list[Table]:
    return [t for t in tables if t.name not in NEVER_EXPORT_TABLES]


# The client-export rows of the tables a plain ``traveler_id = $1`` misses: the WHERE clause of the traveller step of
# crm.erase_subject (crm-026 text; the profile token bound as $1 here), the kept signed evidence, and the trips.
CLIENT_WHERE: dict[str, tuple[str, str]] = {
    "traveler_profile": ("traveler_token = $1", "token"),
    "profile_history": ("traveler_token = $1", "token"),
    "trip_party": ("traveler_token = $1", "token"),
    "traveler_relation": ("from_traveler_id = $1 OR to_traveler_id = $1", "traveler"),
    "proposal": (
        "accepted_name_enc IS NOT NULL AND link_id IN (SELECT l.link_id FROM crm.client_link l"
        " WHERE l.traveler_id = $1)",
        "traveler",
    ),
    "signature_request": ("traveler_id = $1 OR signer_traveler_id = $1", "traveler"),
    "signature_event": ("traveler_id = $1 OR signer_traveler_id = $1", "traveler"),
    "meeting_slot": (
        "traveler_id = $1 OR form_submission_id IN (SELECT s.submission_id FROM crm.form_submission s"
        " WHERE s.traveler_id = $1)",
        "traveler",
    ),
    "dispatch_attempt": (
        "dispatch_id IN (SELECT q.dispatch_id FROM crm.dispatch_queue q WHERE q.traveler_id = $1)",
        "traveler",
    ),
    "message": (
        "conversation_id IN (SELECT c.conversation_id FROM crm.conversation c WHERE c.traveler_id = $1)",
        "traveler",
    ),
    # Not in the erase (it refuses while the client is on a live trip, CRM12): the trips the client is on.
    "trip": ("trip_id IN (SELECT trip_id FROM crm.trip_traveler WHERE traveler_id = $1)", "traveler"),
}
# A proposal names the client only through the acceptance (the name they typed; the erase clears just that): a client
# export holds those columns, not the agency's offer body.
CLIENT_COLUMNS: dict[str, frozenset[str]] = {
    "proposal": frozenset(
        {
            "proposal_id",
            "tenant_id",
            "trip_id",
            "version",
            "link_id",
            "sent_at",
            "accepted_at",
            "accepted_name_enc",
            "accepted_name_erased_at",
            "currency",
            "accepted_total_amount",
            "accepted_deposit_amount",
            "accepted_terms_doc_id",
        }
    )
}


def traveler_query(table: Table) -> tuple[str, str] | None:
    """(WHERE clause, parameter kind) of a client export for ``table``; None = the table holds nothing about one client.
    Parameter kind ``traveler`` binds the traveller id, ``token`` the decrypted F2 profile token."""
    if table.name in CLIENT_WHERE:
        return CLIENT_WHERE[table.name]
    if "traveler_id" in set(table.columns):
        return "traveler_id = $1", "traveler"
    return None


def client_columns(table: Table) -> list[str]:
    """The exported columns of ``table`` in a client export (all, except for the tables of :data:`CLIENT_COLUMNS`)."""
    only = CLIENT_COLUMNS.get(table.name)
    columns = table.exported_columns()
    return columns if only is None else [c for c in columns if c in only]


def json_value(value: Any) -> Any:
    """A database value as JSON (ids and times as strings, decimals as strings so no precision is lost, bytes as
    base64)."""
    import base64

    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, list | tuple):
        return [json_value(v) for v in value]
    return value


def parse_json_column(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value
