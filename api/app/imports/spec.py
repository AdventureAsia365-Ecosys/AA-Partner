"""The four import files (AA-962, design v3 §2.7 "import CSV (clients -> relationships -> trips -> notes)", R6) and
the cleaning of their cells.

Each entity names its required columns, the columns of which at least one must be present, the optional ones and the
header aliases agencies' old tools use. :func:`check_columns` is the card's **R** rule: a file without a required
column is refused (422 ``missing_columns``, the missing names listed) before anything is read or written.

Cell values are cleaned by the same rules as the API's own forms (:func:`app.travellers.fields.clean_f1` for the
client fields); a value that does not pass makes the row ``invalid`` with the code ``invalid_<column>``: the agency
fixes the file and imports it again (the rows already in are skipped, nothing is doubled).
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field

from app.errors import Invalid
from app.travellers.fields import CHANNELS, RELATION_KINDS

ENTITIES = ("clients", "relationships", "trips", "notes")


@dataclass(frozen=True)
class Spec:
    entity: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    any_of: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def columns(self) -> tuple[str, ...]:
        return (*self.required, *self.any_of, *self.optional)


SPECS: dict[str, Spec] = {
    "clients": Spec(
        "clients",
        required=("client_ref", "name"),
        optional=(
            "given_name",
            "family_name",
            "email",
            "phone",
            "preferred_channel",
            "dob",
            "nationality",
            "marketing_consent",
        ),
        aliases={
            "client_id": "client_ref",
            "customer_id": "client_ref",
            "ref": "client_ref",
            "display_name": "name",
            "full_name": "name",
            "client_name": "name",
            "first_name": "given_name",
            "last_name": "family_name",
            "surname": "family_name",
            "e_mail": "email",
            "email_address": "email",
            "mobile": "phone",
            "phone_number": "phone",
            "date_of_birth": "dob",
            "birthday": "dob",
            "marketing": "marketing_consent",
            "channel": "preferred_channel",
        },
    ),
    "relationships": Spec(
        "relationships",
        required=("client_ref", "related_client_ref", "relation"),
        optional=(),
        aliases={"kind": "relation", "relationship": "relation", "related_ref": "related_client_ref"},
    ),
    "trips": Spec(
        "trips",
        required=("trip_ref", "client_ref", "title"),
        optional=("start_date", "end_date", "status", "adults", "companion_refs"),
        aliases={
            "trip_id": "trip_ref",
            "lead_client_ref": "client_ref",
            "trip_name": "title",
            "stage": "status",
            "start": "start_date",
            "end": "end_date",
            "companions": "companion_refs",
        },
    ),
    "notes": Spec(
        "notes",
        required=("text",),
        optional=("note_ref", "date"),
        any_of=("client_ref", "trip_ref"),
        aliases={"body": "text", "note": "text", "note_id": "note_ref", "created": "date"},
    ),
}

# Trip statuses an import may set: past trips arrive closed, open leads at Inquiry. A trip that is booked or under way
# is entered in the pipeline (its money and gates are not in a file).
TRIP_STATUSES = ("completed", "cancelled", "lost", "inquiry")
_STATUS_ALIASES = {
    "done": "completed",
    "complete": "completed",
    "travelled": "completed",
    "canceled": "cancelled",
    "open": "inquiry",
    "lead": "inquiry",
    "new": "inquiry",
}
_TRUE = {"yes", "y", "true", "1", "x", "có", "co"}
_FALSE = {"no", "n", "false", "0", "không", "khong"}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EXCEL_EPOCH = dt.date(1899, 12, 30)
MAX_REF = 100
MAX_NOTE = 10_000
MAX_TITLE = 120
MAX_COMPANIONS = 20


def resolve_headers(spec: Spec, headers: tuple[str, ...]) -> dict[str, str]:
    """``file header -> spec column`` for the headers the spec knows (an alias maps to its column; a column named
    both directly and by an alias keeps the direct one)."""
    out: dict[str, str] = {}
    for h in headers:
        if h in spec.columns:
            out[h] = h
    for h in headers:
        target = spec.aliases.get(h)
        if target and target not in out.values():
            out[h] = target
    return out


def check_columns(spec: Spec, headers: tuple[str, ...]) -> dict[str, list[str]]:
    """The **R** rule: 422 ``missing_columns`` (with the missing names) when a required column, or every column of
    ``any_of``, is absent. Returns the known and ignored columns for the report."""
    mapping = resolve_headers(spec, headers)
    present = set(mapping.values())
    missing = [c for c in spec.required if c not in present]
    if spec.any_of and not present.intersection(spec.any_of):
        missing.append(" or ".join(spec.any_of))
    if missing:
        raise Invalid("missing_columns", "the file lacks a required column", columns=missing)
    return {"columns": sorted(present), "ignored_columns": sorted(h for h in headers if h not in mapping)}


def cells(spec: Spec, raw: dict[str, str]) -> dict[str, str]:
    """A row's cells under the spec's column names (unknown columns dropped)."""
    mapping = resolve_headers(spec, tuple(raw))
    return {target: raw[h] for h, target in mapping.items()}


# ── value cleaners (raise ValueError; the engine turns it into invalid_<column>) ─────────────────────
def text(value: str, max_len: int) -> str:
    out = unicodedata.normalize("NFC", _CONTROL.sub("", value)).strip()
    if not out or len(out) > max_len:
        raise ValueError
    return out


def ref(value: str) -> str:
    return " ".join(text(value, MAX_REF).split())


def flag(value: str) -> bool | None:
    v = value.strip().lower()
    if not v:
        return None
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError


def channel(value: str) -> str | None:
    v = value.strip().lower()
    if not v:
        return None
    if v not in CHANNELS:
        raise ValueError
    return v


def relation(value: str) -> str:
    v = value.strip().lower()
    if v not in RELATION_KINDS:
        raise ValueError
    return v


def date(value: str) -> dt.date | None:
    """ISO ``2024-05-01``, day first ``01/05/2024`` / ``1.5.2024``, or an Excel serial number (a date cell of an
    .xlsx without its style)."""
    v = value.strip()
    if not v:
        return None
    if v.isdigit() and 1 <= int(v) <= 80_000:
        return EXCEL_EPOCH + dt.timedelta(days=int(v))
    try:
        return dt.date.fromisoformat(v[:10])
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", v)
    if not match:
        raise ValueError
    day, month, year = (int(x) for x in match.groups())
    return dt.date(year, month, day)


def trip_status(value: str) -> str | None:
    v = value.strip().lower()
    if not v:
        return None
    v = _STATUS_ALIASES.get(v, v)
    if v not in TRIP_STATUSES:
        raise ValueError
    return v


def adults(value: str) -> int | None:
    v = value.strip()
    if not v:
        return None
    n = int(v)
    if not 1 <= n <= 50:
        raise ValueError
    return n


def refs(value: str) -> list[str]:
    parts = [p for p in re.split(r"[;|]", value) if p.strip()]
    if len(parts) > MAX_COMPANIONS:
        raise ValueError
    return list(dict.fromkeys(ref(p) for p in parts))
