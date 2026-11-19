"""Fields of a quote item (``crm.option``) an edit sets beside its money: the price rules used, the AA title lock and
the agency's display note.

AA-1144 (S285 decision D9, advisory r3 permissions P7): Adventure Asia books exactly the service an AA item names, so
the agency never renames it (403 ``aa_title_read_only``; sending the same name is not a rename). Instead the agency
adds one display note line per item (``crm.option.display_note``, crm-081: 1..200 characters, one line), shown under
the item in the builder, on the client page and carried into the next version of the quote. An agency item keeps its
editable title and may carry a note too.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from app.errors import Forbidden, Invalid

NOTE_MAX = 200  # technical-constant: crm-081 option_display_note_chk (one line under an item, like the title's 200)
NOTE_COPY_SQL = "UPDATE crm.option SET display_note = $2 WHERE option_id = $1"


def rules_json(value: Any) -> str | None:
    """``option.price_rules`` for a write: the rules used (a list), as stored (JSON text), or None."""
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def clean_note(value: Any) -> str | None:
    """The note as stored: whitespace collapsed to one line; None / blank clears it. 422 ``invalid_display_note`` for a
    non-text value or a control character, ``display_note_too_long`` over NOTE_MAX characters."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid("invalid_display_note")
    clean = " ".join(value.split())
    if any(unicodedata.category(c) == "Cc" for c in clean):
        raise Invalid("invalid_display_note")
    if len(clean) > NOTE_MAX:
        raise Invalid("display_note_too_long", f"a note is one line of at most {NOTE_MAX} characters")
    return clean or None


def note_after(changes: dict[str, Any], option: dict[str, Any]) -> str | None:
    """The note an edit leaves on the item: the cleaned new one when the edit sends it, else the current one."""
    return clean_note(changes["display_note"]) if "display_note" in changes else option.get("display_note")


def check_title(option: dict[str, Any], changes: dict[str, Any]) -> None:
    """403 ``aa_title_read_only`` when an edit gives an AA item another name (whitespace aside)."""
    if option.get("source") != "aa" or "title" not in changes:
        return
    sent = changes["title"]
    if not isinstance(sent, str) or " ".join(sent.split()) != " ".join(str(option.get("title") or "").split()):
        raise Forbidden("aa_title_read_only", "an Adventure Asia item keeps its name; add a note line under it instead")


async def copy_note(conn: Any, option_id: Any, source: dict[str, Any]) -> None:
    """A new quote version keeps the note of the item it copies (the INSERT shared by every add path has no note)."""
    if source.get("display_note"):
        await conn.execute(NOTE_COPY_SQL, option_id, source["display_note"])
