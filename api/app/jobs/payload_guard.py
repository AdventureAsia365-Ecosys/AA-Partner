"""Ids only in ``crm.job`` (data standards DM-6.3; design v3 §3.1 "crm.job, crm.outbox and events carry ids only";
card CRM-05 E check "a job payload with a ``pii:`` key fails CI").

A payload or a result is a JSON object whose keys are all either

- an **id key**: ``<name>_id`` or ``<name>_ids``, with a UUID / integer value (or a list of them), or
- a **control key** from :data:`CONTROL_KEYS` or ending in ``_count`` / ``_at`` / ``_days``, with a short token value
  (letters, digits, ``_ . : -``; at most :data:`MAX_TOKEN_CHARS`), a number, a boolean or null; a nested object is
  checked by the same rules.

Anything else — a column name tagged ``pii:`` such as ``email``, ``phone`` or ``passport_number``, a free-text value
with a space or an ``@`` — raises :class:`PersonalDataInPayload`. The check runs at enqueue time
(:func:`app.jobs.queue.enqueue`) and on every handler result (the worker fails the job permanently), and
``tests/unit/test_job_payload_guard.py`` also reads every ``enqueue(...)`` call in ``app/`` and checks its literal keys,
so a new call site with a personal-data key fails CI before it runs.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

ID_KEY = re.compile(r"^[a-z][a-z0-9_]*_ids?$")
CONTROL_SUFFIX = re.compile(r"^[a-z][a-z0-9_]*_(count|at|days)$")
CONTROL_KEYS = frozenset(
    {
        "attempt",
        "batch",
        "dry_run",
        "kind",
        "limit",
        "mode",
        "purpose",
        "reason_code",
        "region",  # AA-962: the ISO country code of an import's local phone numbers (VN, AU)
        "scope",
        "since",
        "slot",
        "status",
        "until",
        "version",
    }
)
TOKEN_VALUE = re.compile(r"^[A-Za-z0-9_.:-]*$")
MAX_TOKEN_CHARS = 64
MAX_DEPTH = 4
MAX_LIST = 1000


class PersonalDataInPayload(ValueError):
    """A job payload or result holds a key or value that could carry personal data."""


def check_payload(payload: Any, *, what: str = "payload") -> None:
    if not isinstance(payload, dict):
        raise PersonalDataInPayload(f"{what} must be a JSON object")
    _check_object(payload, what, 0)


def _check_object(obj: dict[str, Any], path: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise PersonalDataInPayload(f"{path}: nested deeper than {MAX_DEPTH}")
    for key, value in obj.items():
        where = f"{path}.{key}"
        if not isinstance(key, str):
            raise PersonalDataInPayload(f"{path}: non-string key")
        if ID_KEY.match(key):
            _check_id_value(value, where)
        elif key in CONTROL_KEYS or CONTROL_SUFFIX.match(key):
            _check_control_value(value, where, depth)
        else:
            # Never echo the value; the key is code, not data.
            raise PersonalDataInPayload(f"{where}: not an id or control key (ids only, DM-6.3)")


def _check_id_value(value: Any, where: str) -> None:
    if isinstance(value, list):
        if len(value) > MAX_LIST:
            raise PersonalDataInPayload(f"{where}: more than {MAX_LIST} ids")
        for item in value:
            _check_id_value(item, where)
        return
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return
    if isinstance(value, str):
        try:
            uuid.UUID(value)
        except ValueError:
            if not (value.isdigit() and len(value) <= 20):
                raise PersonalDataInPayload(f"{where}: an id must be a UUID or an integer") from None
        return
    raise PersonalDataInPayload(f"{where}: an id must be a UUID or an integer")


def _check_control_value(value: Any, where: str, depth: int) -> None:
    if value is None or isinstance(value, bool | int | float):
        return
    if isinstance(value, str):
        if len(value) > MAX_TOKEN_CHARS or not TOKEN_VALUE.match(value):
            raise PersonalDataInPayload(f"{where}: control values are short tokens (no spaces, no '@')")
        return
    if isinstance(value, dict):
        _check_object(value, where, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_LIST:
            raise PersonalDataInPayload(f"{where}: list longer than {MAX_LIST}")
        for item in value:
            _check_control_value(item, where, depth)
        return
    raise PersonalDataInPayload(f"{where}: unsupported value type")
