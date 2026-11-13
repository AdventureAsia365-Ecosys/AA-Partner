"""AA-1096 L4: ``app/platform_settings/reader.py`` is a byte copy of AA-CIS-App ``shared/settings/reader.py``.

The S279 thresholds audit (section 5.2) wants one reader library: CIS owns it, the other apps vendor a byte copy (like
``app/aa_llm``). ``api/settings_reader.lock.json`` names the canonical repo, path and the CIS commit it was copied from,
and the sha256 of the bytes; this test fails when the copy is edited here. To update: copy the CIS file at a newer
commit, then write that commit and the new sha256 into the lock.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

API = Path(__file__).resolve().parents[2]
COPY = API / "app" / "platform_settings" / "reader.py"
LOCK = API / "settings_reader.lock.json"


def test_the_copy_matches_the_lock() -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    assert hashlib.sha256(COPY.read_bytes()).hexdigest() == lock["sha256"]


def test_the_lock_names_the_canonical_reader_and_a_cis_commit() -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    assert lock["canonical"] == {"repo": "AdventureAsia365-Ecosys/AA-CIS-App", "path": "shared/settings/reader.py"}
    assert lock["dest"] == "api/app/platform_settings/reader.py"
    assert re.fullmatch(r"[0-9a-f]{40}", lock["ref"])


def test_the_copy_imports_no_cis_module() -> None:
    text = COPY.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+(shared|services|api)\b", text, re.M)
