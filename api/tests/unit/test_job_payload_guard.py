"""Ids only in crm.job payloads and results (DM-6.3; card CRM-05 E check "a job payload with a pii: key fails CI").

Two layers: the runtime guard (``app.jobs.payload_guard``, called by ``enqueue`` and on every handler result), and a
static scan of every ``enqueue(...)`` call in ``app/`` whose payload is a dict literal, so a new call site with a
personal-data key fails here before it ever runs.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.jobs.payload_guard import PersonalDataInPayload, check_payload

APP = Path(__file__).resolve().parents[2] / "app"
UUID = "00000000-0000-4000-8000-000000000001"

# Column names the design tags pii: (design v3 §3.3 groups B, D, G) and other personal-data shapes.
PII_KEYS = ["email", "phone", "full_name", "first_name", "passport_number", "date_of_birth", "address", "note", "body"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tenant_id": UUID},
        {"trip_ids": [UUID, UUID], "dry_run": True, "limit": 500},
        {"member_id": 42, "since": "2026-10-01", "reason_code": "owner_request"},
        {"scope": {"traveler_id": UUID, "mode": "erase"}, "requested_at": "2026-10-23T01:02:03Z"},
    ],
)
def test_ids_and_control_keys_pass(payload: dict) -> None:
    check_payload(payload)


@pytest.mark.parametrize("key", PII_KEYS)
def test_a_pii_key_is_refused(key: str) -> None:
    with pytest.raises(PersonalDataInPayload, match=key):
        check_payload({"tenant_id": UUID, key: "x"})


@pytest.mark.parametrize(
    "payload",
    [
        {"tenant_id": "not-a-uuid"},
        {"member_id": "someone@example.com"},
        {"reason_code": "free text with spaces"},
        {"reason_code": "someone@example.com"},
        {"mode": "x" * 65},
        {"scope": {"email": "x"}},
        {"tenant_ids": [UUID, "Nguyen Van A"]},
        {"limit": {"a": {"b": {"c": {"d": {"mode": 1}}}}}},
        [UUID],
    ],
)
def test_values_that_could_carry_personal_data_are_refused(payload: object) -> None:
    with pytest.raises(PersonalDataInPayload):
        check_payload(payload)


def test_the_error_never_echoes_the_value() -> None:
    with pytest.raises(PersonalDataInPayload) as info:
        check_payload({"reason_code": "someone@example.com"})
    assert "someone@example.com" not in str(info.value)


def _enqueue_payload_literals() -> list[tuple[str, int, ast.Dict]]:
    found = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name != "enqueue":
                continue
            payload = (
                node.args[2]
                if len(node.args) > 2
                else next((k.value for k in node.keywords if k.arg == "payload"), None)
            )
            if isinstance(payload, ast.Dict):
                found.append((str(path.relative_to(APP.parent)), node.lineno, payload))
    return found


def test_every_enqueue_literal_in_app_has_only_allowed_keys() -> None:
    literals = _enqueue_payload_literals()
    assert literals, "the scan found no enqueue(...) call: it would pass vacuously"
    for where, line, payload in literals:
        keys = [k.value for k in payload.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        assert len(keys) == len(payload.keys), f"{where}:{line}: payload keys must be string literals"
        check_payload(dict.fromkeys(keys), what=f"{where}:{line}")


def test_the_scan_catches_a_pii_key() -> None:
    """Not vacuous: the same check on a literal with a pii key fails."""
    node = ast.parse('queue.enqueue(pool, "crm_x", {"tenant_id": t, "email": e})').body[0].value
    keys = [k.value for k in node.args[2].keys]
    with pytest.raises(PersonalDataInPayload):
        check_payload(dict.fromkeys(keys))
