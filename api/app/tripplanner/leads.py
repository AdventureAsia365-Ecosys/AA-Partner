"""``crm_tp_lead_pull`` (AA-1038 R5 slice 3, root ADR 0019 d11): the CRM consumer of ``lead.submitted.v1``.

A traveller's "Send to advisor" on an agency's TripPlanner site becomes one lead for THAT agency. The CRM holds no grant
on the tripplanner schema (root ADR 0008a), so it pulls over HTTP: TripPlanner's ``/trip/m2m/v1`` (contract
``docs/contracts/lead.submitted.v1.openapi.yaml`` of the root repo), M2M token of the CRM client, scope
``tripplanner.leads/deliver``, one agency per call.

One run (a due-now maintenance hook every :data:`SLOT_SECONDS`, :class:`LeadPullSchedule`; not deploy-gate or DB
Migrate exempt: a row exists only while due or running):

1. Not configured (``TRIPPLANNER_API_URL`` or ``M2M_CLIENT`` empty) -> ``not_configured``, nothing else.
2. For every agency the CRM holds (``registry.agencies``), at most :data:`PAGE` leads:
   ``GET <TRIPPLANNER_API_URL>/trip/m2m/v1/tenants/{agency}/leads``. An HTTP / token failure is counted
   (``error_class``) and the run goes on with the next agency, then ends **successfully** (the next slot retries).
3. Each lead, in one :func:`app.tenancy.system_tx` of that agency (actor ``job:crm_tp_lead_pull``):
   - **another tenant's lead is refused** (``lead.tenant.tenant_id`` must be the agency of the call; TripPlanner never
     lists one, this is the second check): not stored, not acknowledged, counted ``foreign``;
   - the answers are mapped onto the Stage 1 inquiry (:func:`stage1_raw`) and validated by the same validator as the
     hosted form (:func:`app.intake.stage1.validate`); an optional answer that does not validate (a past start date, a
     phone the CRM does not accept) is dropped, a required one rejects the lead;
   - stored through the inquiry form path (:func:`app.intake.public.store_submission`: vault-encrypted payload, the
     ``crm_intake_process`` job queued in the same transaction), idempotent on ``tp-lead:<lead_id>`` (a lead pulled
     twice stores once); the agency's system form "Trip planner" (lead source ``tripplanner``, disabled for the
     public, created on the first lead: :func:`planner_form`) is the submission's form, so the agency sets the default
     owner of planner leads in Settings > Forms;
   - then acknowledged (``POST .../leads/{lead_id}/ack``): ``stored`` (also for a duplicate) or ``rejected`` with the
     reason; TripPlanner then redacts its copy. An ack that fails is retried by the next run (the store is idempotent).
   A locked or offboarding agency is skipped (its leads wait at TripPlanner, whose hold window ends them).

Payloads and results are counts and error classes only (DM-6.3); no name, e-mail or lead content is logged.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
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

FORM_SQL = (
    "SELECT intake_form_id FROM crm.intake_form WHERE lead_source = $1 AND created_by = $2 AND deleted_at IS NULL "
    "ORDER BY created_at LIMIT 1"
)
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


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def message_text(lead: dict[str, Any]) -> str:
    """What the agent reads first on the trip: the planned trip and the traveller's own words (at most 2000 chars)."""
    trip = lead.get("trip") or {}
    lines = [f"Trip planner request: {trip.get('days', '?')} day(s) in {', '.join(trip.get('countries') or [])}."]
    for tour in trip.get("tours") or []:
        lines.append(f"Tour: {tour.get('title')} ({tour.get('source_id')})")
    for day in trip.get("day_plan") or []:
        lines.append(f"Day {day.get('day')}: {', '.join(day.get('stops') or [])}")
    if trip.get("message"):
        lines += ["", "Message from the traveller:", str(trip["message"])]
    text = "\n".join(lines)
    return text if len(text) <= stage1.MAX_MESSAGE else text[: stage1.MAX_MESSAGE - 1] + "…"


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
    """Validated Stage 1 answers, dropping optional answers that do not validate; LeadRejected otherwise."""
    raw = stage1_raw(lead, today=today)
    for _ in range(2):
        try:
            return stage1.validate(raw, honeypot_field="website", today=today)
        except stage1.ConsentMissing:
            raise LeadRejected("consent_missing") from None
        except stage1.Stage1Invalid as exc:
            optional = [f for f in exc.fields if f in OPTIONAL_FIELDS]
            if len(optional) != len(exc.fields):
                raise LeadRejected("invalid_lead") from None
            for name in optional:
                raw[name] = [] if name == "child_ages" else None
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
    if resp.status != 200:
        raise HttpError(f"tripplanner_http_{resp.status}")
    try:
        return json.loads(resp.body)
    except ValueError:
        raise HttpError("tripplanner_bad_json") from None


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


async def pull_agency(
    pool: asyncpg.Pool,
    cfg: LeadPullConfig,
    keys: KeyProvider,
    tenant_id: uuid.UUID,
    counts: Counter[str],
    *,
    today: dt.date | None = None,
) -> None:
    for lead in await fetch_leads(cfg, tenant_id):
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
                counts["skipped_agency"] += 1  # locked, offboarding or gone: its leads wait at TripPlanner
            except (HttpError, TokenError) as exc:
                errors[getattr(exc, "error_class", type(exc).__name__)] += 1
            except Exception as exc:  # noqa: BLE001 — one agency's error must not hold every later agency
                errors[type(exc).__name__] += 1
                log.error("tp_lead_pull_failed tenant_id=%s error_class=%s", agency.tenant_id, type(exc).__name__)
        if sum(counts.values()) or errors:
            log.info(
                "tp_lead_pull agencies=%d %s errors=%s",
                agencies,
                " ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                dict(errors),
            )
        # Ids and counts only (app.jobs.payload_guard): the error classes are in the log line; the result carries their
        # count and the most frequent one as a code.
        return {
            "status": "ok" if not errors else "partial",
            "agency_count": agencies,
            **{f"{k}_count": v for k, v in sorted(counts.items())},
            "error_count": sum(errors.values()),
            "reason_code": errors.most_common(1)[0][0][:64] if errors else None,
        }


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
