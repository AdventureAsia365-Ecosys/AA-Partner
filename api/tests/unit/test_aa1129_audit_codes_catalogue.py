"""AA-1129 (S285): every audit code the API writes is in the web's catalogue, so the access log words it.

The access log (Settings > Access log, ``web/lib/accessLog.ts``) turns each event into a sentence from the codes'
messages (``web/lib/i18nAuditLog.ts``); the web unit test ``tests/accessLogSentences.test.ts`` checks every code in
``web/lib/auditCodes.json`` has words in English and Vietnamese. This test closes the loop on the API side: a new
``ctx.audit("new_code", …)`` fails here until ``python scripts/audit_codes.py --write`` adds it to the catalogue (and
the web test then asks for its words). Before S285 an unknown code printed raw ("ops read the agency bab16039").
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

API = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("audit_codes", API / "scripts" / "audit_codes.py")
assert _spec is not None and _spec.loader is not None
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)


def test_every_code_the_api_writes_is_in_the_web_catalogue() -> None:
    codes, _ = ac.collect()
    catalogue = ac.load_catalogue()
    assert set(catalogue) == set(ac.KINDS)
    for kind in ac.KINDS:
        missing = sorted(codes[kind] - set(catalogue[kind]))
        assert not missing, (
            f"{kind} {missing}: run python3 scripts/audit_codes.py --write, then word them in web/lib/i18nAuditLog.ts"
        )


def test_the_catalogue_is_sorted_codes() -> None:
    catalogue = ac.load_catalogue()
    for kind in ac.KINDS:
        values = catalogue[kind]
        assert values == sorted(set(values)), kind
        assert all(v.replace("_", "").isalnum() and v == v.lower() for v in values), kind


def _calls(src: str) -> list[dict[str, list[str]]]:
    tree = ast.parse(src)
    res = ac._Resolver(ac._module_constants(tree), {})
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ac._is_audit_call(node):
            args = ac._audit_args(node)
            out.append({k: res.codes(v, "t") for k, v in args.items() if v is not None})
    return out


def test_the_reader_resolves_the_shapes_the_api_uses() -> None:
    src = """
TABLE = "campaign"
class Action(StrEnum):
    TRIP_EDIT = "trip_edit"
ctx.audit("view", "allowed", TABLE, "x", fields=("email_enc", "phone"), reason="manual")
ctx.audit(Action.TRIP_EDIT.value, "denied", "trip", reason="auto" if a else "agent")
ctx.audit(f"refund_{'approve' if ok else 'reject'}", "allowed", "refund_request")
ctx.audit("tag_bulk_" + ("add" if a else "remove"), "allowed", "tag")
ctx.audit("agency_create", "agency", "x")
audit("key_destroy", "allowed", "tenant_key", reason="crypto_shred")
AuditEvent(actor_kind="user", actor_ref="r", action="sign_in", outcome="allowed", object_kind="member")
"""
    calls = _calls(src)
    assert calls[0] == {
        "action": ["view"],
        "object_kind": ["campaign"],
        "fields": ["email_enc", "phone"],
        "reason": ["manual"],
    }
    assert calls[1] == {"action": ["trip_edit"], "object_kind": ["trip"], "reason": ["auto", "agent"]}
    assert calls[2]["action"] == ["refund_approve", "refund_reject"]
    assert calls[3]["action"] == ["tag_bulk_add", "tag_bulk_remove"]
    assert calls[4] == {"action": ["agency_create"], "object_kind": ["agency"]}  # the platform shape (no outcome)
    assert calls[5]["reason"] == ["crypto_shred"]
    assert calls[6] == {"action": ["sign_in"], "object_kind": ["member"]}


def test_write_merges_and_sorts(tmp_path: Path) -> None:
    path = tmp_path / "codes.json"
    path.write_text('{"actions": ["zeta"], "object_kinds": [], "fields": [], "reasons": []}', encoding="utf-8")
    ac.write_catalogue({"actions": {"alpha"}, "object_kinds": {"trip"}, "fields": set(), "reasons": {"manual"}}, path)
    assert ac.load_catalogue(path) == {
        "actions": ["alpha", "zeta"],
        "object_kinds": ["trip"],
        "fields": [],
        "reasons": ["manual"],
    }
