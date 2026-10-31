"""AA-642 S8 (root ADR 0009 decision 13): the vendored AI gateway core ``app/aa_llm`` is an untouched copy.

``app/aa_llm`` is a byte copy of AA-CIS-App ``shared/aa_llm`` pinned by ``aa_llm.lock.json`` (written by CIS
``.github/scripts/llm-core-sync.sh``). The CRM vendors the code only: it keeps its own control plane and ledger in
``crm`` (root ADR 0008a §5-6) and never reads ``shared.llm_*`` or writes ``shared.llm_call_log``. Offline, no DB:
the copy matches its MANIFEST.json, the lock names that manifest, the canonical repo / path, this destination and a
CIS commit (``"ref": "local"`` only with AA_LLM_LOCK_REF_LOCAL_OK=1, the CIS consumers check of an unmerged change),
and nothing in ``app/`` outside the copy uses its shared-ledger SQL. Never edit the copy: change AA-CIS-App, merge,
then sync.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

API = Path(__file__).resolve().parents[2]
CORE = API / "app" / "aa_llm"
LOCK = API / "aa_llm.lock.json"


def _check_copy():
    spec = importlib.util.spec_from_file_location("_aa_llm_check_copy", CORE / "check_copy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_copy_matches_its_manifest_and_the_lock():
    assert _check_copy().problems(CORE, LOCK) == []


def test_the_lock_names_the_canonical_core_and_a_cis_commit():
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    assert lock["canonical"] == {"repo": "AdventureAsia365-Ecosys/AA-CIS-App", "path": "shared/aa_llm"}
    assert lock["dest"] == "api/app/aa_llm"
    ref = lock["ref"]
    assert re.fullmatch(r"[0-9a-f]{40}", ref) or (ref == "local" and os.environ.get("AA_LLM_LOCK_REF_LOCAL_OK") == "1")


def test_the_core_imports_under_the_app_package():
    from app.aa_llm import budget, errors, ledger

    assert errors.classify("ThrottlingException: Too many requests") == errors.THROTTLED
    assert budget.decide(None, 2.0).reason == budget.REASON_SPEND_UNREADABLE
    assert ledger.failed_outcome("insufficient_quota") == ("refused", "quota_exhausted")


def test_the_crm_never_writes_the_shared_ledger_through_the_core():
    # ADR 0008a: the CRM ledger is crm.llm_call. The core's insert_sql targets shared.llm_call_log; no CRM module may
    # call it or name the CIS contract views.
    offenders = []
    for path in (API / "app").rglob("*.py"):
        if CORE in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "insert_sql(" in text or "v_llm_gateway_config" in text or "v_llm_app_spend_today" in text:
            offenders.append(str(path.relative_to(API)))
    assert offenders == []
