"""The L1 response allow-list on every ``/crm/ops/v1`` response (AA-941, card CRM-13a R: "an L1 response contains no
trip, pipeline or revenue keys"; design v3 S-01 "usage meters only ... never trips or revenue", "no PII").

:func:`app.auth.route_guard.class_router` builds the OPS router with :class:`OpsAllowListRoute` (as it builds AGENCY
routers with :class:`app.margin.MarginFilteredRoute`). After a route returns, every key of its JSON body, at any depth,
must be in :data:`OPS_ALLOWED_KEYS` (an explicit list: a new key is a reviewed change of this file and of the root
contract ``crm.ops.v1``) and must not match :data:`OPS_FORBIDDEN` (the root contract checker's patterns plus money and
personal-data names). A key that breaks either rule:

- on ``test`` / ``local``: the response becomes **500** ``ops_key_not_allowed`` (with the key names), so CI fails;
- elsewhere: the key is dropped, the rest is served, and ``crm_ops_key_dropped`` is logged (key names only).

**Setting values are data, not response keys** (AA-1113 follow-up, S284). A json-type CRM setting holds an object, for
example ``crm.ai.connection_monthly_cap_usd`` = ``{"acc2": 5}``: its inner keys (connection keys here) are the setting's
data, chosen by AA staff in the maker-checker flow, not field names of the contract, so they can never be on the
explicit list (the whole-depth walk used to drop them and serve ``{}``). A route class names the fields whose value is
such data in :attr:`OpsAllowListRoute.opaque_value_fields`; only :class:`OpsSettingsRoute` (the ``/settings`` routes of
:mod:`app.routers.ops_settings`) names any: :data:`SETTING_VALUE_FIELDS` (``value``, ``previous_value``,
``default_value``, ``allowed_values``, wherever they sit in a settings answer: the setting, ``last_change``,
``pending``, ``scheduled``, ``agency_overrides``, ``agency_pending``, ``history``, ``definition``). The field name
itself is still checked as above; inside its value every key, at any depth, is checked against :data:`OPS_FORBIDDEN`
only, never against :data:`OPS_ALLOWED_KEYS`. A forbidden inner key (a trip, money or personal-data name) keeps the
rule above: 500 on test / local, dropped + logged elsewhere. No other route and no other field is exempt.

The route class also answers a request that fails validation itself, with ``{detail: invalid_request, message,
fields}`` and the field names only: FastAPI's own 422 body echoes the submitted input, which on the first-owner invite
would carry the owner's e-mail address back (and into any proxy log). Every ops route also declares a Pydantic
response model with ``extra='forbid'`` (``app.routers.ops``), so a key the model does not name fails before this
filter runs; ``tests/unit/test_ops_contract.py`` calls every ops route and checks every key with the same rules.

Service errors keep their shape ``{detail, message, **extra}`` (``app.errors``): they are raised past this route class
to the app's handler; the contract test checks their keys too.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

# Every key an L1 response or error body may carry (crm.ops.v1). Keep sorted by group; never add a personal-data name.
OPS_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        # errors (app.errors bodies, the validation answer above, service error extras)
        "detail",
        "message",
        "fields",
        "state",
        "member_limit",
        # console
        "can_write",
        "scope",  # 1.12 (AA-1035): all | book | book_pool | none (the caller's tenant scope)
        # agency list / summary (TenantPage, TenantSummary)
        "items",
        "next_cursor",
        "tenant_id",
        "agency_name",
        "is_synthetic",
        "has_owner",
        "holds_travellers",
        "created_at",
        "lock_source",  # 1.2 (AA-978 follow-up): aa_ops | cis_tenant | cis_membership while locked
        "membership_feed_state",  # 1.2: ok | pending | stale | failing | unknown
        "membership_feed_lag_s",  # 1.2: seconds since the last successful CIS feed pull
        # usage meters
        "period_start",
        "period_end",
        "seats_used",
        "seats_allowed",
        "active_members_30d",
        "storage_bytes",
        "inbound_mail_30d",
        "ai_cost_usd_month",
        "ai_budget_usd_month",
        "failed_jobs_24h",
        "last_activity_at",
        # first-owner invite; AA-979 studio invitation (+ reissued: the same CIS invitation sent again)
        "invitation_id",
        "status",
        "expires_at",
        "reissued",
        # entitlements, insights opt-out
        "plan",
        "features_allowed",
        "insights_opt_out",
        "managed_by",
        "opt_out",
        # data requests
        "request_id",
        "kind",
        "raised_at",
        "owners_notified",
        # members, owner recovery
        "members",
        "member_id",
        "role",
        "joined_at",
        "active_owners",
        "recovery",
        "eligible_at",
        "owner_member_id",
        "signed_out",
        # failed jobs, retry
        "job_id",
        "attempt",
        "max_attempts",
        "error_class",
        "finished_at",
        # jobs summary (CIS #975)
        "generated_at",
        "kinds",
        "queued",
        "running",
        "failed_24h",
        "oldest_queued_age_s",
        # LLM usage rollup (CIS #975): stage = the CRM call stage (crm_*), not a pipeline stage
        "from",
        "to",
        "rows",
        "day",
        "stage",
        "model",
        "calls",
        "failed_calls",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        # LLM ledger rows (crm.ops.v1 1.6, AA-964, the CIS Calls tab): ids, codes and numbers only, no text
        "call_id",
        "at",
        "app",
        "provider",
        "family",
        "connection_key",
        "deployment_wire_id",
        "attempt_no",
        "provider_request_id",
        "refusal_code",
        "cache_read_tokens",
        "cache_write_tokens",
        "units",
        "unit_kind",
        "pricing_mode",
        "latency_ms",
        "prompt_hash",
        "prompt_version",
        "is_shadow",
        "is_experiment",
        "scrub_entities",
        # data coverage (crm.ops.v1 1.4, AA-992, CIS /admin/data-coverage): platform-wide counts only
        "as_of",
        "agencies",
        "active",
        "locked",
        "offboarding",
        "offboarded",
        "synthetic",
        "invited",
        "disabled",
        "catalog_cache",
        "served",
        "paused",
        "retired",
        "last_full_pass_at",
        "last_ok_at",
        "failures",
        "last_error_class",
        # AA insights (crm.ops.v1 1.5, AA-966, spec module 17): published cells only, no agency id, no total
        "dimension",
        "quarter",
        "agencies_included",
        "agencies_opted_out",
        "k_min",
        "agencies_min",
        "cells",
        "bucket",
        "traveller_count",
        "agency_count",
        "suppressed_cells",
        # the CRM app profile (crm.ops.v1 1.10, AA-1025 H1c): switches and one budget figure, no staff id
        "ai_enabled",
        "bulk_paused",
        "default_daily_budget_usd",
        "degrade_mode",
        "disabled_reason",
        "updated_at",
        # the admin-editable CRM settings (crm.ops.v1 1.13/1.14, AA-1096, crm-064): definitions, values and their change
        # rows; the staff actors are platform:<staff sub> ids (maker-checker), never an e-mail or a name
        "areas",
        "settings",
        "two_person_approval",
        "reader_error",
        "key",
        "area",
        "label",
        "description",
        "value_type",
        "unit",
        "min_value",
        "max_value",
        "allowed_values",
        "default_value",
        "scope_kind",
        "impact",
        "approval",
        "evidence_required",
        "env_override",
        "cache_s",
        "enforced",
        "value",
        "source",
        "version",
        "overridden_by_env",
        "env_ignored",
        "refused",
        "last_change",
        "pending",
        "history",
        "definition",
        "id",
        "effective_from",
        "proposed_by",
        "proposed_at",
        "reason",
        "evidence_ref",
        "self_approved",
        "decided_by",
        "decided_at",
        "decision_note",
        "previous_value",
        # AA-1096 S280: the agencies with their own value of a platform+tenant key and their open changes, and what a
        # change must carry
        "agency_overrides",
        "agency_pending",
        # S280 UX fix round (1.15): the approved version in force, the scheduled approvals, what an approval superseded
        "approved_version",
        "scheduled",
        "superseded_versions",
        "cancelled_scheduled_versions",
        "reason_min",
        "evidence_min",
        "evidence_max",
        # the operator's guide of a CRM setting (the CIS 416 shape the Settings page reads): plain text per language
        "guide",
        "en",
        "vi",
        "what",
        "raise",
        "lower",
        "when",
        "example",
    }
)

# Root tools/contracts/check_contracts.py POLICY['crm.ops.v1'] (trips, trip counts, revenue, pipeline, margin,
# commission, GMV, booking value) plus agency money and personal data. Word parts are split on "_".
_FORBIDDEN_WORDS = (
    "trip",
    "trips",
    "pipeline",
    "invoice",
    "invoices",
    "payment",
    "payments",
    "price",
    "prices",
    "amount",
    "amounts",
    "net",
    "proposal",
    "proposals",
    "booking",
    "bookings",
    "email",
    "emails",
    "phone",
    "phones",
    "passport",
    "dob",
    "address",
    "addresses",
)
OPS_FORBIDDEN = re.compile(
    r"(^|_)(" + "|".join(_FORBIDDEN_WORDS) + r")(_|$)"
    r"|revenue|margin|commission|gmv"
    r"|(^|_)(display|first|last|full)_name($|_)"
    r"|_enc$|_bidx$"
)
STRICT_ENVIRONMENTS = frozenset({"test", "local"})


# The fields of a settings answer whose value is a setting value (crm.ops.v1 SettingValue, or a list of them): opaque
# data on :class:`OpsSettingsRoute` only (see the module docstring). Each name is itself on OPS_ALLOWED_KEYS.
SETTING_VALUE_FIELDS: frozenset[str] = frozenset({"value", "previous_value", "default_value", "allowed_values"})


def key_allowed(key: str) -> bool:
    return key in OPS_ALLOWED_KEYS and not OPS_FORBIDDEN.search(key.lower())


def key_forbidden(key: str) -> bool:
    """A key that matches a forbidden pattern (the only rule inside an opaque setting value)."""
    return OPS_FORBIDDEN.search(key.lower()) is not None


def bad_keys(value: Any, found: set[str] | None = None, opaque_fields: frozenset[str] = frozenset()) -> set[str]:
    """Every key at any depth that is not allowed; inside the value of a field named in ``opaque_fields`` only the
    forbidden ones."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or not key_allowed(k):
                found.add(str(k))
            if isinstance(k, str) and k in opaque_fields:
                _forbidden_inner_keys(v, found)
            else:
                bad_keys(v, found, opaque_fields)
    elif isinstance(value, list):
        for v in value:
            bad_keys(v, found, opaque_fields)
    return found


def _forbidden_inner_keys(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or key_forbidden(k):
                found.add(str(k))
            _forbidden_inner_keys(v, found)
    elif isinstance(value, list):
        for v in value:
            _forbidden_inner_keys(v, found)


def drop_keys(value: Any, opaque_fields: frozenset[str] = frozenset()) -> Any:
    """A copy of ``value`` without the keys that are not allowed (at any depth); inside the value of a field named in
    ``opaque_fields`` without the forbidden ones only."""
    if isinstance(value, dict):
        return {
            k: (_drop_forbidden_inner(v) if k in opaque_fields else drop_keys(v, opaque_fields))
            for k, v in value.items()
            if isinstance(k, str) and key_allowed(k)
        }
    if isinstance(value, list):
        return [drop_keys(v, opaque_fields) for v in value]
    return value


def _drop_forbidden_inner(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_forbidden_inner(v) for k, v in value.items() if isinstance(k, str) and not key_forbidden(k)}
    if isinstance(value, list):
        return [_drop_forbidden_inner(v) for v in value]
    return value


def invalid_request(exc: RequestValidationError) -> JSONResponse:
    """422 with the names of the fields that failed, never the submitted values."""
    names = sorted(
        {
            str(next((part for part in reversed(err.get("loc", ())) if isinstance(part, str)), "body"))
            for err in exc.errors()
        }
    )
    return JSONResponse(
        status_code=422,
        content={"detail": "invalid_request", "message": "the request does not match crm.ops.v1", "fields": names},
    )


class OpsProblem(BaseModel):
    """Every L1 error body: ``app.errors`` (``{detail, message, **extra}``), the validation answer, a guard refusal
    (``{detail}`` only). Documented on every ops route instead of FastAPI's input-echoing validation schema."""

    model_config = ConfigDict(extra="forbid")
    detail: str
    message: str | None = None
    fields: list[str] | None = None
    state: str | None = None
    member_limit: int | None = None


class OpsAllowListRoute(APIRoute):
    """An APIRoute whose JSON body must pass the L1 allow-list (see the module docstring)."""

    # The fields whose value is opaque data (inner keys checked against OPS_FORBIDDEN only): none on an ops route.
    opaque_value_fields: frozenset[str] = frozenset()

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        responses = dict(kwargs.pop("responses", None) or {})
        responses.setdefault(422, {"model": OpsProblem, "description": "Invalid request (field names only)"})
        responses.setdefault("4XX", {"model": OpsProblem, "description": "Refused"})
        super().__init__(path, endpoint, responses=responses, **kwargs)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        path = self.path
        opaque = self.opaque_value_fields

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except RequestValidationError as exc:
                return invalid_request(exc)
            media = (response.media_type or response.headers.get("content-type") or "").lower()
            body = getattr(response, "body", None)
            if not isinstance(body, bytes | bytearray) or "json" not in media or not body:
                return response
            try:
                data = json.loads(body)
            except ValueError:
                return response
            bad = bad_keys(data, opaque_fields=opaque)
            if not bad:
                return response
            names = sorted(bad)
            environment = getattr(getattr(request.app.state, "settings", None), "environment", "")
            if environment in STRICT_ENVIRONMENTS:
                log.error("crm_ops_key_not_allowed path=%s keys=%s", path, ",".join(names))
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": "ops_key_not_allowed",
                        "message": "an L1 response carried a key outside crm.ops.v1",
                        "fields": names,
                    },
                )
            log.warning("crm_ops_key_dropped path=%s keys=%s", path, ",".join(names))
            headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-length", "content-type"}}
            return Response(
                content=json.dumps(drop_keys(data, opaque), ensure_ascii=False, separators=(",", ":")).encode(),
                status_code=response.status_code,
                headers=headers,
                media_type="application/json",
            )

        return handler


class OpsSettingsRoute(OpsAllowListRoute):
    """The ``/settings`` routes (:mod:`app.routers.ops_settings`): a setting value is opaque data (a json setting's
    object keys are its data, e.g. the connection keys of ``crm.ai.connection_monthly_cap_usd``), still free of any
    forbidden key (see the module docstring)."""

    opaque_value_fields = SETTING_VALUE_FIELDS
