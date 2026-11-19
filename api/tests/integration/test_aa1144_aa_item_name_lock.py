"""AA-1144 (S285 decision D9): an AA item's name is read only in a quote; the agency adds one display note line.

Real PostgreSQL (crm-081 applied by the conftest replay), the builder service as the routes call it:
  - renaming an AA item is 403 aa_title_read_only and changes nothing; sending the same name (any spacing) passes;
  - the note is saved on one line, shown in the builder view, cleared by "" / null, and an over-long note is 422;
  - an agency item keeps an editable title and takes a note too;
  - a new version of the quote keeps the notes.
Invented names only.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.errors import Forbidden, Invalid
from app.proposals import builder
from tests.aa_fakes import FakeBooking
from tests.integration.test_builder_api import (
    _aa,
    _as,
    _block,
    _catalog_tour,
    _create,
    _pid,
    _publishable,
    _setup,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _edit(pool: asyncpg.Pool, s: dict, v: dict, option_id: str, changes: dict) -> dict:
    return await _as(pool, s, "agent", builder.update_option, _pid(v), v["edit_seq"], uuid.UUID(option_id), changes)


async def test_an_aa_item_keeps_its_name_and_takes_a_note(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    await _catalog_tour(app_pool)
    v = await _create(app_pool, s)
    v = await _aa(app_pool, s, FakeBooking(b2b_price="900.00"), v, sell_amount=Decimal("1500"))
    o = _block(v, 1)["options"][0]
    assert o["source"] == "aa" and o["display_note"] is None
    with pytest.raises(Forbidden) as err:
        await _edit(app_pool, s, v, o["option_id"], {"title": "Grand Harbour Palace"})
    assert err.value.code == "aa_title_read_only"
    after = await _as(app_pool, s, "agent", builder.view, _pid(v))
    assert _block(after, 1)["options"][0]["title"] == o["title"] and after["edit_seq"] == v["edit_seq"]

    same_name = {"title": f"  {o['title']} ", "display_note": "Sea view\n on request"}
    v = await _edit(app_pool, s, v, o["option_id"], same_name)
    o = _block(v, 1)["options"][0]
    assert o["title"] == "Everest view 3 days" and o["display_note"] == "Sea view on request"
    with pytest.raises(Invalid) as long:
        await _edit(app_pool, s, v, o["option_id"], {"display_note": "x" * 201})
    assert long.value.code == "display_note_too_long"
    v = await _edit(app_pool, s, v, o["option_id"], {"sell_amount": Decimal("1600")})
    assert _block(v, 1)["options"][0]["display_note"] == "Sea view on request"  # an edit without the note keeps it
    v = await _edit(app_pool, s, v, o["option_id"], {"display_note": ""})
    assert _block(v, 1)["options"][0]["display_note"] is None


async def test_an_agency_item_is_renamed_and_a_new_version_keeps_the_notes(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _publishable(app_pool, s)
    o = _block(v, 1)["options"][0]
    v = await _edit(app_pool, s, v, o["option_id"], {"title": "Lodge by the river", "display_note": "Early check-in"})
    o = _block(v, 1)["options"][0]
    assert (o["title"], o["display_note"]) == ("Lodge by the river", "Early check-in")
    pub = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    v2 = await _create(app_pool, s, from_proposal_id=_pid(pub))
    copied = next(x for x in _block(v2, 1)["options"] if x["title"] == "Lodge by the river")
    assert copied["option_id"] != o["option_id"] and copied["display_note"] == "Early check-in"
