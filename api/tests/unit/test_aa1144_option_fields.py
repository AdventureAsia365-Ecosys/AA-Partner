"""AA-1144 (S285 decision D9): the AA item title lock and the display note (app/proposals/option_fields.py), the route
field and the client page line. The real-PostgreSQL path is tests/integration/test_aa1144_aa_item_name_lock.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.errors import Forbidden, Invalid
from app.proposals import client_page, option_fields
from app.routers.builder import OptionChange
from tests.unit.test_client_proposal import L1, LODGE, NOW, T1, _row, _s

AA = {"source": "aa", "title": "Valley tour 3 days", "display_note": None}
AGENCY = {"source": "agency", "title": "Visa fee", "display_note": "Paid at the border"}


@pytest.mark.parametrize("title", ["Valley tour 3 days", "  Valley  tour 3 days ", "Valley tour\n3 days"])
def test_an_aa_item_may_send_its_own_name(title: str) -> None:
    option_fields.check_title(AA, {"title": title})
    option_fields.check_title(AA, {"display_note": "x"})  # no title sent


@pytest.mark.parametrize("title", ["Grand Harbour Palace", "", None, 7])
def test_an_aa_item_is_never_renamed(title: Any) -> None:
    with pytest.raises(Forbidden) as err:
        option_fields.check_title(AA, {"title": title})
    assert err.value.code == "aa_title_read_only"


def test_an_agency_item_is_renamed_freely() -> None:
    option_fields.check_title(AGENCY, {"title": "Visa on arrival"})


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), ("", None), ("   ", None), ("Sea view", "Sea view"), (" Sea \n view\t", "Sea view"),
     ("x" * 200, "x" * 200)],
)  # fmt: skip
def test_the_note_is_one_clean_line(value: Any, expected: str | None) -> None:
    assert option_fields.clean_note(value) == expected


@pytest.mark.parametrize(
    "value,code",
    [("x" * 201, "display_note_too_long"), (5, "invalid_display_note"), ("bell\x07", "invalid_display_note")],
)
def test_a_bad_note_is_422(value: Any, code: str) -> None:
    with pytest.raises(Invalid) as err:
        option_fields.clean_note(value)
    assert err.value.code == code


def test_note_after_keeps_the_current_note_unless_sent() -> None:
    assert option_fields.note_after({}, AGENCY) == "Paid at the border"
    assert option_fields.note_after({"display_note": None}, AGENCY) is None
    assert option_fields.note_after({"display_note": " New  line "}, AA) == "New line"


def test_rules_json() -> None:
    assert option_fields.rules_json(None) is None
    assert option_fields.rules_json('[{"name": "x"}]') == '[{"name": "x"}]'
    assert option_fields.rules_json([{"name": "x"}]) == '[{"name": "x"}]'


@pytest.mark.asyncio
async def test_copy_note_writes_only_a_note() -> None:
    conn = AsyncMock()
    await option_fields.copy_note(conn, "new-id", AA)
    conn.execute.assert_not_called()
    await option_fields.copy_note(conn, "new-id", AGENCY)
    conn.execute.assert_awaited_once_with(option_fields.NOTE_COPY_SQL, "new-id", "Paid at the border")


def test_the_route_takes_a_note() -> None:
    body = OptionChange.model_validate({"seq": 1, "display_note": "Sea view"})
    assert body.model_dump(exclude_unset=True) == {"seq": 1, "display_note": "Sea view"}


@pytest.mark.asyncio
async def test_the_client_page_shows_the_note() -> None:
    s = _s()
    for o in s["options"]:
        o["display_note"] = "Sea view on request" if o["option_id"] == T1 else None
    page = await client_page.view(object(), object(), _row(), s, [], NOW)  # type: ignore[arg-type]
    notes = {o["option_id"]: o["note"] for d in page["days"] for b in d["blocks"] for o in b["options"]}
    assert notes[str(T1)] == "Sea view on request" and notes[str(L1)] is None
    assert str(LODGE) in {b["option_block_id"] for d in page["days"] for b in d["blocks"]}
