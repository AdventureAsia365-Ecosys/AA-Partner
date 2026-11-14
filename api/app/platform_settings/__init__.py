"""The CRM binding of the settings registry reader (AA-1096 lane L4, crm-064; S279 thresholds audit section 5.2).

    from app import platform_settings as ps
    ps.get(ps.RATE_PUBLIC)                                   # sync: the last snapshot, never the database
    await ps.tenant_get(ctx.conn, ps.AI_BUDGET_MAX, ctx.tenant_id)   # an agency's own value (inside its context)

``reader.py`` beside this file is a byte copy of AA-CIS-App ``shared/settings/reader.py`` (pinned by
``api/settings_reader.lock.json``; ``tests/unit/test_aa1096_settings_reader_copy.py``): never edit it here. The
defaults come from ``compiled_defaults.json``, generated from the crm migration seed by
``scripts/gen_settings_defaults.py`` (CI keeps the two equal).

Resolution of ``get(key, tenant_id=None)``:
  1. the definition's emergency env override, when a definition names one (one release only, tech lead S279 Q5; none
     does since crm-070, AA-1096 l5): the process env var, or the mapping :func:`configure_env` gave (tests). A warning
     is logged and the CIS page shows "overridden by env";
  2. the approved value of the agency (``tenant_id`` given, a ``platform+tenant`` key) whose ``effective_from`` has
     come, from the per-agency cache :func:`tenant_refresh` fills. crm.setting_value is FORCE RLS: an agency's rows are
     seen only inside that agency's context (crm-064), so the process-wide snapshot holds the platform rows only and
     an agency's rows are read through the caller's own connection, inside its own context;
  3. the approved platform value;
  4. the compiled default (today's effective value).

Cache: the platform snapshot is refreshed every ``cache_s`` (30 s) by the background refresher of the API and the
worker (:func:`start_refresher`); an agency's rows are re-read when older than the same window.

Retention switches (tech lead S279 Q7): :func:`dry_run` joins a Terraform floor (``LIFECYCLE_DRY_RUN`` and the others,
env) and the admin switch: a delete runs only when both are off. The floor is never a setting.

A value read here is the platform's policy: never copy it into a module constant at import time
(``tests/unit/test_aa1096_settings_lint.py`` fails on a new policy-looking constant in the governed modules).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.platform_settings.reader import Definition, Resolution, SettingsReader, coerce, parse_text, validate

log = logging.getLogger(__name__)

COMPILED_DEFAULTS = Path(__file__).with_name("compiled_defaults.json")
KEY_PREFIX = "crm."
SCHEMA = "crm"

# The keys code reads (crm-064 seed). tests/unit/test_aa1096_settings_lint.py checks each enforced key has a reader.
AI_BUDGET_MAX = "crm.ai.budget_max_usd"
AI_BUDGET_DEFAULT = "crm.ai.budget_default_usd"
AI_DRAFT_MAX_TOKENS = "crm.ai.draft_reply_max_tokens"
RATE_PUBLIC = "crm.rate.public_per_minute"
RATE_INTAKE = "crm.rate.intake_per_minute"
RATE_INTAKE_FORM = "crm.rate.intake_form_per_hour"
RATE_MEMBER = "crm.rate.member_per_minute"
RATE_AI = "crm.rate.ai_per_minute"
REFUND_THRESHOLD_DEFAULT = "crm.payments.refund_owner_threshold_default"
INVOICE_REMINDER_EVERY_DAYS = "crm.payments.invoice_reminder_every_days"
INVOICE_REMINDER_MAX = "crm.payments.invoice_reminder_max"
DISCOUNT_LIMIT_DEFAULT = "crm.agency_defaults.discount_owner_limit_pct"
MARKUP_MAX = "crm.agency_defaults.markup_max_pct"
PRICE_CHECKS_DEFAULT = "crm.agency_defaults.price_checks_per_minute"
PRICE_CHECKS_MIN = "crm.agency_defaults.price_checks_per_minute_min"
PRICE_CHECKS_MAX = "crm.agency_defaults.price_checks_per_minute_max"
TRASH_DAYS_DEFAULT = "crm.agency_defaults.trash_days"
TRASH_DAYS_MIN = "crm.agency_defaults.trash_days_min"
TRASH_DAYS_MAX = "crm.agency_defaults.trash_days_max"
ARCHIVE_MONTHS_DEFAULT = "crm.agency_defaults.auto_archive_months"
ARCHIVE_MONTHS_MIN = "crm.agency_defaults.auto_archive_months_min"
ARCHIVE_MONTHS_MAX = "crm.agency_defaults.auto_archive_months_max"
LIFECYCLE_DRY_RUN = "crm.retention.lifecycle_dry_run"
JOB_RETENTION_DRY_RUN = "crm.retention.job_dry_run"
AUDIT_RETENTION_DRY_RUN = "crm.retention.audit_dry_run"
PARTNER_CLEANUP_DRY_RUN = "crm.retention.partner_account_cleanup_dry_run"
TWO_PERSON_APPROVAL = "crm.settings.two_person_approval"

# The platform ranges of the agency-owner settings: (new-agency default, lowest an owner may pick, highest). The
# registry service refuses a change that would break min <= default <= max (422 range_inconsistent);
# :func:`int_range` and :func:`clamped_default` are the backstop of a reader that still meets a broken group.
RANGE_GROUPS: tuple[tuple[str, str, str], ...] = (
    (TRASH_DAYS_DEFAULT, TRASH_DAYS_MIN, TRASH_DAYS_MAX),
    (ARCHIVE_MONTHS_DEFAULT, ARCHIVE_MONTHS_MIN, ARCHIVE_MONTHS_MAX),
    (PRICE_CHECKS_DEFAULT, PRICE_CHECKS_MIN, PRICE_CHECKS_MAX),
)

# An agency's approved rows, read in its own context (RLS). One statement, the newest approved row in force per key.
TENANT_SQL = """
SELECT DISTINCT ON (v.key) v.key, v.value::text AS value_text, v.version
  FROM crm.setting_value v
 WHERE v.tenant_id = $1 AND v.status = 'approved' AND v.effective_from <= now()
 ORDER BY v.key, v.effective_from DESC, v.version DESC
"""
TENANT_CACHE_MAX = 5000  # technical-constant: per-process memory bound of the agency cache (oldest dropped)


def load_definitions(path: Path = COMPILED_DEFAULTS) -> list[dict[str, Any]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["definitions"])


# The operator's guide of each key ({"en": {what, raise, lower, when, example}, "vi": ...}), shown by CIS Platform >
# Settings (CRM tab) in the CIS 416 shape. A hand-edited file beside the compiled defaults (crm.setting_definition has
# no guide column; the reader never needs it); tests/unit/test_aa1096_settings_guides.py keeps it complete.
GUIDES_FILE = Path(__file__).with_name("guides.json")
GUIDES: dict[str, dict[str, Any]] = dict(json.loads(GUIDES_FILE.read_text(encoding="utf-8"))["guides"])


def guide(key: str) -> dict[str, Any] | None:
    """The guide of ``key`` (None when it has none yet: the guide lint fails on it)."""
    return GUIDES.get(key)


async def _alert(key: str, problem: str, raw: Any, version: int | None) -> None:
    """A stored value broke its definition: one AA ops e-mail per key and version (the reader logged it already)."""
    from app.observability import ops_alerts

    ops_alerts.emit("setting_value_refused", f"{key}:{version}", key=key, version=version)


READER = SettingsReader(load_definitions(), key_prefix=KEY_PREFIX, schema=SCHEMA, alert=_alert)


@dataclass
class _TenantRows:
    rows: dict[str, tuple[Any, int]]  # key -> (value, version)
    loaded_at: float


_TENANT: dict[str, _TenantRows] = {}
_clock: Callable[[], float] = time.monotonic


def configure_env(environ: Mapping[str, str] | None) -> None:
    """The env the reader reads a definition's emergency override from (tests; None = the process env). Since crm-070
    no definition names one: this only matters for a future one-release override."""
    READER.environ = None if environ is None else dict(environ)


def definition(key: str) -> Definition:
    return READER.definition(key)


def keys() -> list[str]:
    return READER.keys()


def resolve(key: str, tenant_id: uuid.UUID | str | None = None) -> Resolution:
    """The value in force and where it comes from (env / tenant / platform / default)."""
    d = READER.definition(key)
    base = READER.resolve(key)
    if base.source == "env" or tenant_id is None or not d.tenant_scoped:
        return base
    cached = _TENANT.get(str(tenant_id))
    hit = cached.rows.get(key) if cached is not None else None
    if hit is None:
        return base
    return Resolution(key, coerce(d, hit[0]), "tenant", version=hit[1], env_ignored=base.env_ignored)


def get(key: str, tenant_id: uuid.UUID | str | None = None) -> Any:
    """The value in force (sync: the last snapshot and the agency cache, never the database)."""
    return resolve(key, tenant_id).value


async def aget(key: str) -> Any:
    """``get`` after a platform refresh when the snapshot is older than the cache window."""
    await READER.refresh()
    return READER.get(key)


def _tenant_stale(tenant_id: str) -> bool:
    cached = _TENANT.get(tenant_id)
    return cached is None or _clock() - cached.loaded_at >= READER.max_age_s


async def tenant_refresh(conn: Any, tenant_id: uuid.UUID | str, *, force: bool = False) -> bool:
    """Re-read one agency's approved rows through ``conn`` (a connection inside that agency's context: its request,
    job or platform transaction) when older than the cache window. True when fresh after the call. A read error (the
    crm-064 tables missing, a statement error) keeps the last good rows (or none: the platform value applies) and runs
    in a savepoint, so the caller's transaction stays usable."""
    tid = str(tenant_id)
    if not force and not _tenant_stale(tid):
        return True
    try:
        async with conn.transaction():
            fetched = await conn.fetch(TENANT_SQL, uuid.UUID(tid))
    except Exception as exc:  # noqa: BLE001 — a settings read never breaks the request that asked for it
        log.warning("settings_tenant_refresh_failed tenant_id=%s error=%s", tid, type(exc).__name__)
        old = _TENANT.get(tid)
        _TENANT[tid] = _TenantRows(old.rows if old else {}, _clock())  # retry after one window
        return False
    rows: dict[str, tuple[Any, int]] = {}
    for r in fetched:
        try:
            d = READER.definition(r["key"])
        except KeyError:
            continue  # a key a newer migration added
        try:
            value = json.loads(r["value_text"])
        except (TypeError, ValueError):
            value, problem = None, "stored value is not JSON"
        else:
            problem = validate(d, value)
        if problem is None and not d.tenant_scoped:
            problem = f"an agency value of a {d.scope_kind} setting"
        if problem:
            log.error(
                "setting_value_refused key=%s tenant_id=%s version=%s problem=%s", d.key, tid, r["version"], problem
            )
            await _alert(d.key, problem, r["value_text"], r["version"])
            continue
        rows[d.key] = (value, int(r["version"]))
    if len(_TENANT) >= TENANT_CACHE_MAX and tid not in _TENANT:
        _TENANT.pop(next(iter(_TENANT)))
    _TENANT[tid] = _TenantRows(rows, _clock())
    return True


async def tenant_get(conn: Any, key: str, tenant_id: uuid.UUID | str) -> Any:
    """An agency's value of ``key`` (its own override of a platform+tenant key, else the platform value), read through
    ``conn`` inside that agency's context."""
    if READER.definition(key).tenant_scoped:
        await tenant_refresh(conn, tenant_id)
    return get(key, tenant_id)


def forget_tenant(tenant_id: uuid.UUID | str | None = None) -> None:
    """Drop the cached rows of one agency (after an approval in this process), or of every agency."""
    if tenant_id is None:
        _TENANT.clear()
    else:
        _TENANT.pop(str(tenant_id), None)


def invalidate() -> None:
    """The next read of the snapshot and of every agency goes to the database."""
    READER.invalidate()
    forget_tenant()


def as_decimal(value: Any) -> Decimal:
    """A numeric setting as a Decimal without a float tail: 10.0 -> 10, 2.5 -> 2.5 (money, percentages)."""
    number = Decimal(str(value))
    return number.to_integral_value() if number == number.to_integral_value() else number


def decimal(key: str, tenant_id: uuid.UUID | str | None = None) -> Decimal:
    """``get`` as a Decimal (see :func:`as_decimal`)."""
    return as_decimal(get(key, tenant_id))


def range_group(key: str) -> tuple[str, str, str] | None:
    """The (default, min, max) group ``key`` belongs to, or None."""
    return next((g for g in RANGE_GROUPS if key in g), None)


_INVERTED_SEEN: set[tuple[str, int, int]] = set()


def int_range(min_key: str, max_key: str) -> tuple[int, int]:
    """The (lowest, highest) an agency owner may pick. The registry refuses min > max; should a group still be
    inverted (a value written around the service), the two ends are swapped and logged once, so the owner's setting
    stays editable instead of refusing every value."""
    lo, hi = int(get(min_key)), int(get(max_key))
    if lo <= hi:
        return lo, hi
    if (min_key, lo, hi) not in _INVERTED_SEEN:
        _INVERTED_SEEN.add((min_key, lo, hi))
        log.error("setting_range_inverted min_key=%s min=%s max_key=%s max=%s", min_key, lo, max_key, hi)
    return hi, lo


def clamped_default(default_key: str) -> int:
    """The new-agency default of a range group, never outside the range the platform allows today."""
    group = range_group(default_key)
    if group is None or group[0] != default_key:
        raise KeyError(f"{default_key} is not the default of a range group")
    lo, hi = int_range(group[1], group[2])
    return max(lo, min(hi, int(get(default_key))))


def limit(key: str) -> Callable[[], int]:
    """A limiter's limit read at each call (:class:`app.ratelimit.FixedWindowLimiter` takes a callable)."""
    return lambda: int(get(key))


def flag(value: bool | Callable[[], bool]) -> bool:
    """A switch that is a plain bool or a callable read now (:func:`dry_run`)."""
    return bool(value()) if callable(value) else bool(value)


def dry_run(floor: bool, key: str) -> Callable[[], bool]:
    """Two keys on a delete (tech lead S279 Q7): dry while the Terraform floor (env) is on OR the admin switch is on."""
    return lambda: bool(floor) or bool(get(key))


def install(pool: Any) -> None:
    READER.install(pool)


async def _refresh_forever(get_pool: Callable[[], Awaitable[Any]]) -> None:
    while True:  # the pool is created on first use (app.db.Database.pool); a failed connect retries next window
        try:
            READER.install(await get_pool())
            break
        except Exception as exc:  # noqa: BLE001 — the defaults stay in force until the database answers
            log.warning("settings_pool_unavailable error=%s", type(exc).__name__)
            await asyncio.sleep(READER.max_age_s)
    await READER.run_refresher()


def start_refresher(get_pool: Callable[[], Awaitable[Any]]) -> asyncio.Task[None] | None:
    """Refresh the platform snapshot in the background through the pool ``get_pool`` returns (``Database.pool``).
    None under ENVIRONMENT=test: tests read the defaults (or monkeypatch :func:`get`)."""
    if os.environ.get("ENVIRONMENT") == "test":
        return None
    return asyncio.create_task(_refresh_forever(get_pool), name="crm-settings-refresher")


async def stop_refresher(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "COMPILED_DEFAULTS",
    "Definition",
    "GUIDES",
    "KEY_PREFIX",
    "READER",
    "RANGE_GROUPS",
    "Resolution",
    "aget",
    "as_decimal",
    "clamped_default",
    "coerce",
    "configure_env",
    "decimal",
    "definition",
    "dry_run",
    "flag",
    "forget_tenant",
    "get",
    "guide",
    "install",
    "int_range",
    "invalidate",
    "keys",
    "limit",
    "load_definitions",
    "parse_text",
    "range_group",
    "resolve",
    "start_refresher",
    "stop_refresher",
    "tenant_get",
    "tenant_refresh",
    "validate",
]
