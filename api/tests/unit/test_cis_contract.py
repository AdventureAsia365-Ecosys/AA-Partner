"""app.cis.contract against the vendored ``tenant.membership.v1`` (AA-978).

The contract lives in the root repo (``AA-Ecosys/docs/contracts``, private), so CI cannot fetch it: the byte copy in
``docs/contracts/`` is pinned by hash here, and the hand-written checks are compared with the copy's enums and field
lists, then run on the root samples (copied with it) and on mutations of them. A change of the root file means:
copy it again, update the hash, and fix whatever these tests then report.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.cis import contract

DOCS = Path(__file__).resolve().parents[3] / "docs" / "contracts"
SPEC = DOCS / "tenant.membership.v1.openapi.yaml"
SAMPLES = DOCS / "samples" / "tenant.membership.v1"
# sha256 of the root file docs/contracts/tenant.membership.v1.openapi.yaml (S250 root PR R1, the AA-978 text fix, and
# review round 1: a withdrawn invitation has a null identity).
ROOT_CONTRACT_SHA256 = "d05404dac29827fd2267d61923916ebeefff6add1fc22955781d942ebd5154e8"

TENANT = "0b9d6c7e-3f43-4c1e-9a52-6d0c3b8f1a01"


def _sample(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def _schema_block(name: str) -> str:
    """The text of one schema under components/schemas (up to the next schema at the same indent)."""
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(rf"^    {name}:\n((?:      .*\n|\n)+)", text, re.MULTILINE)
    assert match, name
    return match.group(1)


def _list(block: str, key: str) -> set[str]:
    match = re.search(rf"{key}: \[([^\]]*)\]", block, re.DOTALL)
    assert match, key
    return {v.strip() for v in match.group(1).replace("\n", " ").split(",") if v.strip()}


def _properties(block: str) -> set[str]:
    props = block.split("properties:", 1)[1]
    return set(re.findall(r"^        ([a-z_]+):", props, re.MULTILINE))


def _event(**changes: Any) -> dict[str, Any]:
    event = copy.deepcopy(_sample("event-removed-valid.json")["payload"]["items"][0])
    event.update(changes)
    return event


def test_the_vendored_copy_is_the_pinned_root_file() -> None:
    assert hashlib.sha256(SPEC.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256
    assert "x-aa-contract: tenant.membership.v1" in SPEC.read_text(encoding="utf-8")


def test_the_enums_and_fields_match_the_contract() -> None:
    event = _schema_block("MembershipEvent")
    assert _list(event, "enum") == contract.EVENT_TYPES
    assert _list(event, "required") == contract.EVENT_REQUIRED
    assert _properties(event) == contract.EVENT_FIELDS
    membership = _schema_block("Membership")
    assert _list(membership, "required") == contract.MEMBERSHIP_REQUIRED
    assert _properties(membership) == contract.MEMBERSHIP_FIELDS
    assert _list(_schema_block("ContentStudioRole"), "enum") == contract.CONTENT_STUDIO_ROLES
    assert _list(_schema_block("MembershipStatus"), "enum") == contract.MEMBERSHIP_STATUSES
    assert _list(_schema_block("Identity"), "required") == contract.IDENTITY_FIELDS
    assert _list(_schema_block("MembershipEventPage"), "required") == contract.PAGE_FIELDS
    for name in ("MembershipEvent", "Membership", "Identity", "MembershipEventPage"):
        assert "additionalProperties: false" in _schema_block(name), name
    assert "maximum: 500" in SPEC.read_text(encoding="utf-8")  # the page limit the CRM asks for stays under it


def test_the_valid_sample_page_passes() -> None:
    sample = _sample("event-removed-valid.json")
    items, next_cursor = contract.parse_page(sample["payload"])
    assert next_cursor == "m_000042"
    assert [contract.check_event(e) for e in items] == [None]


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        ("membership-granting-crm-role-invalid.json", "crm_role_present"),
        ("membership-with-email-invalid.json", "unknown_field"),
    ],
)
def test_the_invalid_samples_are_refused(sample: str, reason: str) -> None:
    membership = _sample(sample)["payload"]
    assert contract.check_event(_event(membership=membership)) == reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"crm_role": "owner"}, "crm_role_present"),
        ({"note": "x"}, "unknown_field"),
        ({"type": "membership.granted"}, "invalid_type"),
        ({"event_id": "not-a-uuid"}, "invalid_field"),
        ({"cursor": "has space"}, "invalid_field"),
        ({"occurred_at": "2026-11-21"}, "invalid_field"),
        ({"occurred_at": "2026-11-21T09:00:00"}, "invalid_field"),  # no offset
        ({"membership": None}, "membership_missing"),
        ({"membership": "x"}, "invalid_field"),
        ({"tenant_id": "1e2d3c4b-5a69-4788-9a0b-1c2d3e4f5a6b"}, "tenant_mismatch"),
    ],
)
def test_a_broken_event_is_refused_with_its_reason(change: dict[str, Any], reason: str) -> None:
    assert contract.check_event(_event(**change)) == reason


def test_a_missing_field_and_a_non_object() -> None:
    event = _event()
    del event["cursor"]
    assert contract.check_event(event) == "missing_field"
    assert contract.check_event(["x"]) == "invalid_field"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"content_studio_role": "agent"}, "invalid_role"),  # a CRM role name is not a Content studio role
        ({"status": "deleted"}, "invalid_field"),
        ({"status": "active"}, "status_mismatch"),  # a removal event carries the removed row
        ({"identity": {"iss": "https://issuer.example", "sub": "s", "email": "x"}}, "unknown_field"),
        ({"identity": {"iss": "http://issuer.example", "sub": "s"}}, "invalid_field"),
        ({"identity": {"iss": "https://issuer.example", "sub": ""}}, "invalid_field"),
        ({"identity": {"iss": "https://issuer.example"}}, "invalid_field"),
        ({"invitation_id": "x"}, "invalid_field"),
        ({"updated_at": "yesterday"}, "invalid_field"),
        ({"display_name": "A"}, "unknown_field"),
    ],
)
def test_a_broken_membership_is_refused(change: dict[str, Any], reason: str) -> None:
    event = _event()
    event["membership"].update(change)
    assert contract.check_event(event) == reason


def test_a_missing_membership_field() -> None:
    event = _event()
    del event["membership"]["updated_at"]
    assert contract.check_event(event) == "missing_field"


def test_the_withdrawn_invitation_sample_passes() -> None:
    """Review round 1: CIS 276 emits membership.removed with a null identity for an invitation removed before
    activation; that is a valid event (the feed records it ignored / no_identity), not a contract violation."""
    items, _ = contract.parse_page(_sample("event-invitation-withdrawn-valid.json")["payload"])
    assert [contract.check_event(e) for e in items] == [None]


def test_only_an_active_membership_needs_an_identity() -> None:
    event = _event()
    event["membership"]["identity"] = None
    assert contract.check_event(event) is None  # removed, never activated
    activated = _event(type="membership.activated")
    activated["membership"].update(identity=None, status="active")
    assert contract.check_event(activated) == "identity_missing"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"type": ["membership.removed"]}, "invalid_type"),
        ({"type": {"x": 1}}, "invalid_type"),
        ({"type": 7}, "invalid_type"),
    ],
)
def test_a_non_string_type_is_refused_not_raised(change: dict[str, Any], reason: str) -> None:
    """Review round 1: a list or an object is not hashable; a frozenset test must not raise TypeError."""
    assert contract.check_event(_event(**change)) == reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"content_studio_role": ["owner"]}, "invalid_role"),
        ({"content_studio_role": {"r": "owner"}}, "invalid_role"),
        ({"status": ["removed"]}, "invalid_field"),
        ({"status": None}, "invalid_field"),
    ],
)
def test_a_non_string_role_or_status_is_refused_not_raised(change: dict[str, Any], reason: str) -> None:
    event = _event()
    event["membership"].update(change)
    assert contract.check_event(event) == reason


def test_lifecycle_events_need_no_membership_and_invited_needs_no_identity() -> None:
    assert contract.check_event(_event(type="tenant.locked", membership=None)) is None
    event = _event(type="membership.invited")
    event["membership"].update(identity=None, status="invited", invitation_id=None)
    assert contract.check_event(event) is None


@pytest.mark.parametrize(
    "page",
    [
        [],
        {"items": []},
        {"items": [], "next_cursor": "c", "extra": 1},
        {"items": "x", "next_cursor": "c"},
        {"items": [{}] * 501, "next_cursor": "c"},
        {"items": [], "next_cursor": 5},
        {"items": [], "next_cursor": "has space"},
        {"items": [], "next_cursor": "x" * 201},
    ],
)
def test_a_malformed_page_is_refused_whole(page: Any) -> None:
    with pytest.raises(contract.PageError):
        contract.parse_page(page)


def test_an_empty_page_may_keep_an_empty_cursor() -> None:
    assert contract.parse_page({"items": [], "next_cursor": ""}) == ([], "")


def test_the_sample_tenant_matches_the_fixture() -> None:
    assert _event()["tenant_id"] == TENANT
