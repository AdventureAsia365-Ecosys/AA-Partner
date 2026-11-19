"""What an agency may sell of its own (AA-1144, root ADR 0019 b6 as amended; crm-079).

Business rule of 19/11/2026 (S285 decisions B5, B9, B10): every product (hotel and extra nights, transfer and
vehicle, guide, ticket and entrance, meal and restaurant, activity, DMC) is Adventure Asia's and its suppliers'; the
agency only sells it. The agency's own items are a closed list, typed by hand, with no external integration and
never sent to AA:

- ``flight``: a ticket the agency issues itself (GDS, airline portal, consolidator);
- ``visa``: the visa service (the fee passed through, the agency's handling);
- ``insurance``: travel insurance the agency sells for an insurer;
- ``fee``: the agency's own service fee.

One place holds the list so the proposal builder, the booking lines, the supplier directory, the service catalogue
and the AI supplier confirmation all refuse the same things with the same words. The database holds the same lists
(crm-079 CHECKs); "other" is a supplier category only (a consolidator, the agency itself), never an item type.
"""

from __future__ import annotations

from typing import Any

from app.errors import Invalid

# The item types (crm.option.category, crm.booking_line.category; crm.agency_service.category).
CATEGORIES = ("flight", "visa", "insurance", "fee")
# The own supplier categories (crm.agency_supplier.category; the Adventure Asia row is "dmc").
SUPPLIER_CATEGORIES = ("flight", "insurance", "visa", "other")
# The builder blocks an agency item may sit on: the "Other" block (agency services). A lodging, activity, transport,
# meal or guide block takes Adventure Asia items only (the AA drawer).
AGENCY_BLOCK_TYPES = ("other",)

CLOSED_LIST = "flight, visa, travel insurance or the agency fee"
AA_PRODUCTS = (
    "hotels and extra nights, transfers and vehicles, guides, tickets, meals and activities come from Adventure Asia"
)


def category(value: Any, *, field: str = "category") -> str:
    """The item type of a new agency item or line (required): 422 ``invalid_category`` outside the closed list."""
    if value not in CATEGORIES:
        raise Invalid(
            "invalid_category",
            f"an agency item is a {CLOSED_LIST}; {AA_PRODUCTS}",
            field=field,
            allowed=list(CATEGORIES),
        )
    return str(value)


def supplier_category(value: Any) -> str:
    """The category of an own supplier: 422 ``invalid_category`` for a hotel, transfer, guide, restaurant, activity or
    DMC supplier (those are Adventure Asia's suppliers)."""
    if value not in SUPPLIER_CATEGORIES:
        raise Invalid(
            "invalid_category",
            f"an own supplier provides a {CLOSED_LIST} (or other); {AA_PRODUCTS}",
            allowed=list(SUPPLIER_CATEGORIES),
        )
    return str(value)


def block_takes_agency_items(block_type: str) -> None:
    """422 ``agency_item_not_allowed`` when an agency item would sit on a lodging, activity, transport, meal or guide
    block: those take Adventure Asia items only (B11: extra hotel nights are asked from AA)."""
    if block_type not in AGENCY_BLOCK_TYPES:
        raise Invalid(
            "agency_item_not_allowed",
            f"a {block_type} block takes Adventure Asia items only; add your {CLOSED_LIST} on an Other block",
            block_type=block_type,
        )


def block_type_change(new_type: str, block: dict[str, Any], options: list[dict[str, Any]]) -> None:
    """A block that holds agency items keeps its type "other" (no way round :func:`block_takes_agency_items`)."""
    if new_type == block["type"]:
        return
    if any(o["source"] == "agency" for o in options if o["option_block_id"] == block["option_block_id"]):
        block_takes_agency_items(new_type)
