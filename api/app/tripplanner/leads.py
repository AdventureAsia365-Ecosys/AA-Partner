"""``crm_tp_lead_pull`` (AA-1038 R5 slice 3, root ADR 0019 d11): the CRM consumer of ``lead.submitted.v1``.

A traveller's "Send to advisor" on an agency's TripPlanner site becomes one lead for THAT agency. The CRM holds no grant
on the tripplanner schema (root ADR 0008a), so it pulls over HTTP: TripPlanner's ``/trip/m2m/v1`` (contract
``docs/contracts/lead.submitted.v1.openapi.yaml`` of the root repo), M2M token of the CRM client, scope
``tripplanner.leads/deliver``, one agency per call.

One run (a due-now maintenance hook every :data:`SLOT_SECONDS`, :class:`LeadPullSchedule`; not deploy-gate or DB
Migrate exempt: a row exists only while due or running):

1. Not configured (``TRIPPLANNER_API_URL`` or ``M2M_CLIENT`` empty) -> ``not_configured``, nothing else.
2. For every agency the CRM holds (``registry.agencies``) whose TripPlanner leads come to this CRM
   (:func:`routed_here`: ``crm.tenant_entitlement.tp_crm_leads``, crm-047, the projection of the tenant's CIS
   ``tp.hosted`` + ``crm.core`` keys by ``crm_entitlement_pull``; every other agency is counted ``skipped_not_routed``
   with no HTTP call), at most :data:`PAGE` leads: ``GET <TRIPPLANNER_API_URL>/trip/m2m/v1/tenants/{agency}/leads``.
   TripPlanner's 404 ``tenant_not_on_crm`` / ``unknown_tenant`` (its own route says no, e.g. between a CIS change and
   the next projection) is a counted skip (``skipped_not_on_crm``), not an error. Any other HTTP / token failure is
   counted (``error_class``) and the run goes on with the next agency, then ends **successfully** (the next slot
   retries). Every run logs ONE summary line (``tp_lead_pull_summary``: agencies, pulled leads, skips, errors).
3. Each lead, in one :func:`app.tenancy.system_tx` of that agency (actor ``job:crm_tp_lead_pull``):
   - **another tenant's lead is refused** (``lead.tenant.tenant_id`` must be the agency of the call; TripPlanner never
     lists one, this is the second check): not stored, not acknowledged, counted ``foreign``;
   - the traveller's name, e-mail and phone are checked first with the contract's ONE Traveller rule
     (:func:`traveller_errors`, the same rule and the same shared test vectors as TripPlanner's ``validate``: root
     ``docs/contracts/rules/lead.submitted.v1.traveller-rules.json``, byte copy in ``docs/contracts/rules/``), so a lead
     TripPlanner accepted is never rejected here for its name or e-mail; the rule includes what the intake job needs
     (a name with at least one letter or digit token, :func:`app.intake.stage1.has_name_token`), so a lead stored and
     acknowledged ``stored`` here never fails later at ``crm_intake_process`` for its traveller: a name like ``-`` is
     acknowledged ``rejected`` (alert below) instead;
   - the answers are mapped onto the Stage 1 inquiry (:func:`stage1_raw`) and validated by the same validator as the
     hosted form (:func:`app.intake.stage1.validate`); an optional answer that does not validate (a past start date, a
     phone the CRM cannot normalise) is dropped, a required one rejects the lead; a dropped phone is kept as text on
     the inquiry message (the contract pins E.164, but a number the CRM's phone library rejects is still the
     traveller's);
   - stored through the inquiry form path (:func:`app.intake.public.store_submission`: vault-encrypted payload, the
     ``crm_intake_process`` job queued in the same transaction), idempotent on ``tp-lead:<lead_id>`` (a lead pulled
     twice stores once); the agency's system form "Trip planner" (lead source ``tripplanner``, disabled for the
     public, created on the first lead: :func:`planner_form`) is the submission's form, so the agency sets the default
     owner of planner leads in Settings > Forms;
   - then acknowledged (``POST .../leads/{lead_id}/ack``): ``stored`` (also for a duplicate) or ``rejected`` with the
     reason; TripPlanner then redacts its copy. An ack that fails is retried by the next run (the store is idempotent).
     A rejected lead is a traveller's request the agency never sees: it raises the ops alert ``tp_lead_rejected``
     (:mod:`app.observability.ops_alerts`: ids and the reason code only, one e-mail per lead) and an ERROR line, and
     TripPlanner's daily run reports it too (``lead_rejected_by_crm``).
   The agency is entered (a :func:`app.tenancy.system_tx` that reads :func:`routed_here`) BEFORE its leads are
   listed. A locked, offboarding or unknown agency is skipped without a list call, and the list call is TripPlanner's
   per-tenant beacon: TripPlanner takes crm leads for a tenant only while the CRM listed it within
   ``TP_LEAD_CRM_PULL_FRESH_S`` (10 minutes), so a tenant this CRM cannot serve gets 503 ``lead_channel_unavailable``
   at TripPlanner and nothing is stored there. Leads stored before the agency was locked wait at TripPlanner, whose
   hold (at most 7 days) ends them.

Payloads and results are counts and error classes only (DM-6.3); no name, e-mail or lead content is logged.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.audit_export import registry
from app.cis.http import DEFAULT_TIMEOUT_S, HttpError, Transport, urllib_transport
from app.cis.m2m import TokenError, TokenProvider
from app.errors import Forbidden, NotFound
from app.intake import forms, stage1
from app.intake.public import store_submission
from app.jobs import queue
from app.jobs.kinds import TP_LEAD_PULL
from app.jobs.queue import Job
from app.observability import ops_alerts
from app.tenancy import SystemContext, system_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

ACTOR = f"job:{TP_LEAD_PULL}"
SCOPE = "tripplanner.leads/deliver"
CONTRACT = "lead.submitted.v1"
LEAD_SOURCE = "tripplanner"
FORM_LABEL = "Trip planner"
SLOT_SECONDS = 60
MAX_ATTEMPTS = 1
HISTORY_DAYS = 7
PAGE = 20
SCHEDULER = "scheduler:crm_tp_lead_pull"

# Stage 1 destination codes by the country names the AA catalogue writes (anything else: not_sure).
COUNTRY_CODES = {
    "vietnam": "vietnam",
    "viet nam": "vietnam",
    "laos": "laos",
    "cambodia": "cambodia",
    "thailand": "thailand",
    "myanmar": "myanmar",
    "burma": "myanmar",
    "bhutan": "bhutan",
    "nepal": "nepal",
    "indonesia": "indonesia",
}
OPTIONAL_FIELDS = ("phone", "start_date", "end_date", "nights", "message", "child_ages", "locale")

# The contract's Traveller rule (lead.submitted.v1), the same on both sides: control characters removed, Unicode NFC,
# trimmed, then the length in code points and the pattern; a name must also hold at least one name token (a letter or
# a digit after NFKD), which is what crm_intake_process needs to file the traveller (vault.name_index ->
# blind_index.name_tokens). Shared test vectors: docs/contracts/rules/ (root copy is the source); TripPlanner
# backend/assembly/leads.py applies the same constants.
TRAVELLER_CONTROL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
TRAVELLER_MAX = {"name": 120, "email": 254, "phone": 40}
TRAVELLER_EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,63}$")
TRAVELLER_PHONE = re.compile(r"^\+[1-9][0-9 ()-]{5,39}$")

FORM_SQL = (
    "SELECT intake_form_id FROM crm.intake_form WHERE lead_source = $1 AND created_by = $2 AND deleted_at IS NULL "
    "ORDER BY created_at LIMIT 1"
)
# crm-047: the agency's TripPlanner leads come to this CRM (CIS tp.hosted + crm.core, projected by
# crm_entitlement_pull). No row (never projected) = not routed here.
ROUTED_SQL = "SELECT coalesce((SELECT tp_crm_leads FROM crm.tenant_entitlement WHERE deleted_at IS NULL), false)"
# TripPlanner's 404 codes that mean "this tenant's leads do not go to a CRM" (serve_m2m / load_route), not a failure.
NOT_ON_CRM_CODES = frozenset({"tenant_not_on_crm", "unknown_tenant"})
INSERT_FORM_SQL = """
INSERT INTO crm.intake_form (public_key, label, form_definition_id, honeypot_field, lead_source, status)
VALUES ($1, $2, $3, $4, 'tripplanner', 'disabled')
RETURNING intake_form_id
"""


@dataclass(frozen=True)
class LeadPullConfig:
    base_url: str
    tokens: TokenProvider
    transport: Transport = field(default=urllib_transport)
    timeout_s: float = DEFAULT_TIMEOUT_S


class LeadRejected(ValueError):
    """The lead can never be stored (a required answer does not validate). ``reason`` is a code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ForeignLead(ValueError):
    """The lead names another tenant than the agency it was pulled for."""


class TenantNotOnCrm(HttpError):
    """TripPlanner answered 404 ``tenant_not_on_crm`` / ``unknown_tenant``: its route does not send this tenant's leads
    to a CRM. A counted skip, not an error."""

    def __init__(self) -> None:
        super().__init__("tripplanner_tenant_not_on_crm")


def _rule_text(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    text = unicodedata.normalize("NFC", TRAVELLER_CONTROL.sub("", value)).strip()
    if len(text) > max_len:
        raise ValueError
    return text or None


def traveller_errors(traveller: Any) -> list[str]:
    """The Traveller fields that break the contract's rule (``name`` and ``email`` required, ``phone`` optional E.164).
    Pure; the same verdict as TripPlanner's ``validate`` on every shared test vector."""
    t = traveller if isinstance(traveller, dict) else {}
    bad: list[str] = []
    checks = {
        "name": lambda v: v is not None and stage1.has_name_token(v),
        "email": lambda v: v is not None and TRAVELLER_EMAIL.match(v) is not None,
        "phone": lambda v: v is None or TRAVELLER_PHONE.match(v) is not None,
    }
    for name, ok in checks.items():
        try:
            value = _rule_text(t.get(name), TRAVELLER_MAX[name])
            if name == "email" and value is not None:
                # Lower-case FIRST, then the length and the pattern (the producer's order; it sends that value):
                # U+0130 lower-cases to two code points (S272 review 4, vectors dotted_capital_i_*).
                value = value.lower()
                if len(value) > TRAVELLER_MAX[name]:
                    raise ValueError
            if not ok(value):
                bad.append(name)
        except ValueError:
            bad.append(name)
    return bad


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def message_text(lead: dict[str, Any], *, unchecked_phone: str | None = None) -> str:
    """What the agent reads first on the trip: the planned trip and the traveller's own words (at most 2000 chars).

    ``unchecked_phone``: the phone the traveller typed when the CRM could not normalise it (no region to read it with,
    or a number ``phonenumbers`` does not know); it goes on the second line.

    The traveller's own part comes FIRST: the header line, the phone line, then "Message from the traveller:" and
    ``trip.message``. The catalog lines (one ``Tour:`` per tour, one ``Day N:`` per day) come last and are the only
    part cut to fit, so a long catalog never cuts the traveller's words or phone (S272 review: 4 tours x 14 days cut
    a wheelchair request away for good). Every part is cleaned with Stage 1's own steps first; parts are joined by
    newlines, which never compose under NFC, so the joined text is already what Stage 1 stores."""
    trip = lead.get("trip") or {}
    limit = stage1.MAX_MESSAGE
    header = _clean(
        f"Trip planner request: {trip.get('days', '?')} day(s) in {', '.join(trip.get('countries') or [])}."
    )
    phone = _clean(f"Phone as typed (not checked): {unchecked_phone}") if unchecked_phone else ""
    own = _clean(f"Message from the traveller:\n{trip['message']}") if trip.get("message") else ""
    catalog_lines = [f"Tour: {tour.get('title')} ({tour.get('source_id')})" for tour in trip.get("tours") or []]
    catalog_lines += [
        f"Day {day.get('day')}: {', '.join(day.get('stops') or [])}" for day in trip.get("day_plan") or []
    ]
    catalog = _clean("\n".join(catalog_lines))

    phone_cost = len(phone) + 1 if phone else 0
    if own:
        # Only a message beyond the contract (1000) can reach this: keep room for the phone and some of the header.
        own = _fit_message(own, limit - phone_cost - min(len(header), _HEADER_FLOOR) - 2)
    own_cost = len(own) + 2 if own else 0
    # The header keeps at least min(len(header), _HEADER_FLOOR) by the cut above, so it is never empty.
    text = "\n".join(p for p in (_fit_message(header, limit - phone_cost - own_cost), phone) if p)
    if own:
        text = f"{text}\n\n{own}"
    room = limit - len(text) - 2
    if catalog and room > 1:
        text = f"{text}\n\n{_fit_message(catalog, room)}"
    return text


def _phone_only_message(phone: str) -> str:
    """The message when Stage 1 refused the built one and now refuses the phone: only the phone, as typed."""
    return _fit_message(f"Phone as typed (not checked): {phone}")


# No bound: stage1.clean_text only cleans (control characters out, NFC, trimmed) when the limit cannot be reached.
_UNBOUNDED = 2**62
# The part of the header line kept when an over-long message must be cut (only beyond the contract's 1000).
_HEADER_FLOOR = 200


def _clean(text: str) -> str:
    return stage1.clean_text(text, _UNBOUNDED) or ""


def _fit_message(text: str, limit: int = stage1.MAX_MESSAGE) -> str:
    """``text`` cleaned with Stage 1's own steps (:func:`app.intake.stage1.clean_text`) and THEN cut to ``limit``
    code points (default ``stage1.MAX_MESSAGE``), so Stage 1 ``validate`` never finds it too long and drops it. NFC can
    lengthen a catalog name (U+0958..U+095F, U+0F73, U+FB1D..U+FB4E, U+0344 decompose), so the cut must come after it;
    the cut text is cleaned again (a cut inside a combining sequence may re-normalise) and shortened until it fits."""
    clean = _clean(text)
    if len(clean) <= limit:
        return clean
    if limit < 1:
        return ""
    cut = limit - 1
    while cut > 0:
        out = _clean(clean[:cut] + "…")
        if len(out) <= limit:
            return out
        cut -= 1
    return "…"


def stage1_raw(lead: dict[str, Any], *, today: dt.date | None = None) -> dict[str, Any]:
    """The lead as the answers of the hosted Stage 1 form (the CRM's one inquiry shape)."""
    traveller, party, trip = lead.get("traveller") or {}, lead.get("party") or {}, lead.get("trip") or {}
    consent = lead.get("consent") or {}
    codes = [COUNTRY_CODES.get(str(c).strip().lower()) for c in trip.get("countries") or []]
    destinations = list(dict.fromkeys(c for c in codes if c)) or ["not_sure"]
    start = trip.get("start_date")
    if start and str(start) < stage1.earliest_travel_day(today).isoformat():
        start = None  # a date already too close or past: the agent asks again
    days = trip.get("days")
    raw: dict[str, Any] = {
        "destinations": destinations,
        "start_date": start,
        "nights": days - 1 if isinstance(days, int) and 2 <= days <= stage1.MAX_NIGHTS + 1 else None,
        "adults": party.get("adults"),
        "child_ages": party.get("child_ages") or [],
        "budget_band": trip.get("budget_band") or "not_sure",
        "name": traveller.get("name"),
        "email": traveller.get("email"),
        "phone": traveller.get("phone"),
        "message": message_text(lead),
        "consent_contact": consent.get("contact") is True,
        "consent_marketing": consent.get("marketing") is True,
        "locale": traveller.get("locale") if traveller.get("locale") in stage1.LOCALES else "en",
        "client_submission_id": lead.get("lead_id"),
    }
    return raw


def answers_of(lead: dict[str, Any], *, today: dt.date | None = None) -> stage1.Stage1Answers:
    """Validated Stage 1 answers, dropping optional answers that do not validate; LeadRejected otherwise.

    The contract's Traveller rule runs first (:func:`traveller_errors`): a name or e-mail that breaks it rejects the
    lead (TripPlanner refuses the same values before storing, so this is a producer bug, never a traveller's typo); a
    phone that breaks it is kept as text on the message, like a phone the CRM cannot normalise."""
    errors = traveller_errors(lead.get("traveller"))
    if {"name", "email"} & set(errors):
        raise LeadRejected("invalid_lead")
    raw = stage1_raw(lead, today=today)
    if "phone" in errors:
        typed = (lead.get("traveller") or {}).get("phone")
        raw["phone"] = None
        if isinstance(typed, str) and typed.strip():
            raw["message"] = message_text(lead, unchecked_phone=TRAVELLER_CONTROL.sub("", typed).strip()[:40])
    dropped: set[str] = set()
    # Each failed round drops at least one more optional answer, so the loop ends within len(OPTIONAL_FIELDS) + 1.
    for _ in range(len(OPTIONAL_FIELDS) + 1):
        try:
            return stage1.validate(raw, honeypot_field="website", today=today)
        except stage1.ConsentMissing:
            raise LeadRejected("consent_missing") from None
        except stage1.Stage1Invalid as exc:
            optional = [f for f in exc.fields if f in OPTIONAL_FIELDS]
            if not optional or len(optional) != len(exc.fields):
                raise LeadRejected("invalid_lead") from None
            keep_phone_text: str | None = None
            if "phone" in optional and raw.get("phone"):
                # Never lose the traveller's phone: keep it as text where the agent reads first (AA-1038 review),
                # written BEFORE the optional answers are dropped. A message Stage 1 refused (now or before) is never
                # rebuilt (that made the next round refuse it again, S272); the phone alone takes its place.
                phone_text = str(raw["phone"])[:40]
                if "message" not in optional and "message" not in dropped:
                    raw["message"] = message_text(lead, unchecked_phone=phone_text)
                else:
                    keep_phone_text = _phone_only_message(phone_text)
            for name in optional:
                raw[name] = [] if name == "child_ages" else None
                dropped.add(name)
            if keep_phone_text is not None:
                raw["message"] = keep_phone_text
    raise LeadRejected("invalid_lead")


async def planner_form(ctx: SystemContext) -> uuid.UUID:
    """The agency's system intake form of planner leads (created on first use, disabled for the public)."""
    found = await ctx.conn.fetchval(FORM_SQL, LEAD_SOURCE, ACTOR)
    if found:
        return found
    definition_id = await forms.ensure_stage1_definition(ctx)
    created = await ctx.conn.fetchval(INSERT_FORM_SQL, secrets.token_urlsafe(16), FORM_LABEL, definition_id, "website")
    ctx.audit("intake_form_created", "allowed", "intake_form", str(created), reason="tripplanner_leads")
    return created


async def ingest(ctx: SystemContext, vault: Vault, lead: dict[str, Any], *, today: dt.date | None = None) -> str:
    """Store one lead in the agency's intake. Returns ``stored`` or ``duplicate``; raises ForeignLead / LeadRejected."""
    tenant = (lead.get("tenant") or {}).get("tenant_id")
    if str(tenant) != str(ctx.tenant_id):
        raise ForeignLead("foreign_lead")
    if lead.get("contract") != CONTRACT:
        raise LeadRejected("unknown_contract")
    try:
        lead_id = uuid.UUID(str(lead.get("lead_id")))
    except ValueError:
        raise LeadRejected("invalid_lead") from None
    answers = answers_of(lead, today=today)
    form_id = await planner_form(ctx)
    stored = await store_submission(
        ctx, vault, form_id, answers, idempotency_key=f"tp-lead:{lead_id}", created_by=ACTOR
    )
    return "stored" if stored is not None else "duplicate"


def _url(cfg: LeadPullConfig, tenant_id: uuid.UUID, *rest: str) -> str:
    return "/".join([cfg.base_url.rstrip("/"), "trip/m2m/v1/tenants", str(tenant_id), "leads", *rest])


async def _call(cfg: LeadPullConfig, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    token = await cfg.tokens.token(SCOPE)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    resp = await cfg.transport(method, url, headers, data, cfg.timeout_s)
    if resp.status == 401:
        cfg.tokens.invalidate(SCOPE)
    if resp.status == 404 and _error_code(resp.body) in NOT_ON_CRM_CODES:
        raise TenantNotOnCrm
    if resp.status != 200:
        raise HttpError(f"tripplanner_http_{resp.status}")
    try:
        return json.loads(resp.body)
    except ValueError:
        raise HttpError("tripplanner_bad_json") from None


def _error_code(body: bytes) -> str | None:
    """The ``error`` code of a TripPlanner error body (``{"error": "<code>"}``), else None."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    code = parsed.get("error") if isinstance(parsed, dict) else None
    return code if isinstance(code, str) else None


async def routed_here(ctx: SystemContext) -> bool:
    """True when the agency's TripPlanner leads come to this CRM (``tp_crm_leads``, crm-047; RLS: the context's
    agency)."""
    return bool(await ctx.conn.fetchval(ROUTED_SQL))


async def fetch_leads(cfg: LeadPullConfig, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    page = await _call(cfg, "GET", _url(cfg, tenant_id) + f"?limit={PAGE}")
    leads = page.get("leads") if isinstance(page, dict) else None
    if not isinstance(leads, list):
        raise HttpError("tripplanner_bad_page")
    return [x for x in leads if isinstance(x, dict)]


async def acknowledge(
    cfg: LeadPullConfig, tenant_id: uuid.UUID, lead_id: str, outcome: str, reason: str | None = None
) -> None:
    await _call(cfg, "POST", _url(cfg, tenant_id, lead_id, "ack"), {"outcome": outcome, "reason": reason})


def _lead_ref(lead_id: str) -> str:
    """The lead id as a UUID for logs and alerts (anything else becomes ``invalid``: never echo a payload value)."""
    try:
        return str(uuid.UUID(lead_id))
    except ValueError:
        return "invalid"


async def pull_agency(
    pool: asyncpg.Pool,
    cfg: LeadPullConfig,
    keys: KeyProvider,
    tenant_id: uuid.UUID,
    counts: Counter[str],
    *,
    today: dt.date | None = None,
) -> None:
    # Enter the agency first: a locked / offboarding / unknown agency raises Forbidden / NotFound here, BEFORE the list
    # call, so TripPlanner's per-tenant beacon goes stale and it refuses that tenant's crm leads (nothing stored). An
    # agency whose TripPlanner leads do not come here (crm-047) is skipped with no HTTP call.
    async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
        routed = await routed_here(ctx)
    if not routed:
        counts["skipped_not_routed"] += 1
        return
    page = await fetch_leads(cfg, tenant_id)
    counts["listed"] += 1
    counts["pulled"] += len(page)
    for lead in page:
        lead_id = str(lead.get("lead_id"))
        try:
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                outcome = await ingest(ctx, Vault(keys, ctx), lead, today=today)
        except ForeignLead:
            counts["foreign"] += 1
            log.error("tp_lead_foreign tenant_id=%s", tenant_id)
            continue
        except LeadRejected as exc:
            counts["rejected"] += 1
            # A traveller's request the agency will never see: fail loudly (ids and the reason code only, DM-6.3).
            ref = _lead_ref(lead_id)
            log.error("tp_lead_rejected tenant_id=%s lead_id=%s reason=%s", tenant_id, ref, exc.reason)
            ops_alerts.emit("tp_lead_rejected", ref, tenant_id=tenant_id, lead_id=ref, reason=exc.reason)
            await acknowledge(cfg, tenant_id, lead_id, "rejected", exc.reason)
            continue
        counts[outcome] += 1
        await acknowledge(cfg, tenant_id, lead_id, "stored")
        counts["acknowledged"] += 1


class LeadPull:
    """The ``crm_tp_lead_pull`` handler (``config`` None = not configured)."""

    def __init__(
        self, config: LeadPullConfig | None, keys: KeyProvider | None, today: Callable[[], dt.date] = _today
    ) -> None:
        self._config = config
        self._keys = keys
        self._today = today

    async def __call__(self, pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        if self._config is None or self._keys is None:
            log.info("tp_lead_pull not_configured")
            return {"status": "not_configured"}
        counts: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        agencies = 0
        for agency in await registry.agencies(pool):
            agencies += 1
            try:
                await pull_agency(pool, self._config, self._keys, agency.tenant_id, counts, today=self._today())
            except (Forbidden, NotFound):
                # Locked, offboarding or gone: not listed, so TripPlanner stops taking its crm leads (stale beacon).
                counts["skipped_agency"] += 1
            except TenantNotOnCrm:
                # TripPlanner's route says this tenant's leads go elsewhere (or nowhere): a skip, not an error.
                counts["skipped_not_on_crm"] += 1
            except (HttpError, TokenError) as exc:
                errors[getattr(exc, "error_class", type(exc).__name__)] += 1
            except Exception as exc:  # noqa: BLE001 — one agency's error must not hold every later agency
                errors[type(exc).__name__] += 1
                log.error("tp_lead_pull_failed tenant_id=%s error_class=%s", agency.tenant_id, type(exc).__name__)
        log.info(summary_line(agencies, counts, errors))
        # Ids and counts only (app.jobs.payload_guard): the error classes are in the log line; the result carries their
        # count and the most frequent one as a code.
        return {
            "status": "ok" if not errors else "partial",
            "agency_count": agencies,
            **{f"{k}_count": v for k, v in sorted(counts.items())},
            "error_count": sum(errors.values()),
            "reason_code": errors.most_common(1)[0][0][:64] if errors else None,
        }


SKIPS = ("skipped_not_routed", "skipped_not_on_crm", "skipped_agency")


def summary_line(agencies: int, counts: Counter[str], errors: Counter[str]) -> str:
    """The one log line of a run: agencies, pulled leads, skips (total and per reason) and errors (total and per class).
    Counts and error classes only (DM-6.3)."""
    parts = [f"agencies={agencies}", f"listed={counts['listed']}", f"pulled={counts['pulled']}"]
    parts.append(f"skipped={sum(counts[k] for k in SKIPS)}")
    parts += [f"{k}={counts[k]}" for k in SKIPS]
    parts += [f"{k}={v}" for k, v in sorted(counts.items()) if k not in (*SKIPS, "listed", "pulled")]
    parts.append(f"errors={sum(errors.values())}")
    if errors:
        parts.append("error_classes=" + ",".join(f"{k}:{v}" for k, v in sorted(errors.items())))
    return "tp_lead_pull_summary " + " ".join(parts)


def schedule_key(now: dt.datetime) -> str:
    return f"{TP_LEAD_PULL}:slot:{int(now.timestamp()) // SLOT_SECONDS}"


class LeadPullSchedule:
    """Worker maintenance hook: one ``crm_tp_lead_pull`` run per :data:`SLOT_SECONDS` slot, due now, never ahead."""

    def __init__(self, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            TP_LEAD_PULL,
            {"purpose": "slot_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, TP_LEAD_PULL, HISTORY_DAYS)
        self._done_slot = key
        return job_id
