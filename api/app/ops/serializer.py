"""The L1 response allow-list on every ``/crm/ops/v1`` response (AA-941, card CRM-13a R: "an L1 response contains no
trip, pipeline or revenue keys"; design v3 S-01 "usage meters only ... never trips or revenue", "no PII").

:func:`app.auth.route_guard.class_router` builds the OPS router with :class:`OpsAllowListRoute` (as it builds AGENCY
routers with :class:`app.margin.MarginFilteredRoute`). After a route returns, every key of its JSON body, at any depth,
must be in :data:`OPS_ALLOWED_KEYS` (an explicit list: a new key is a reviewed change of this file and of the root
contract ``crm.ops.v1``) and must not match :data:`OPS_FORBIDDEN` (the root contract checker's patterns plus money and
personal-data names). A key that breaks either rule:

- on ``test`` / ``local``: the response becomes **500** ``ops_key_not_allowed`` (with the key names), so CI fails;
- elsewhere: the key is dropped, the rest is served, and ``crm_ops_key_dropped`` is logged (key names only).

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


def key_allowed(key: str) -> bool:
    return key in OPS_ALLOWED_KEYS and not OPS_FORBIDDEN.search(key.lower())


def bad_keys(value: Any, found: set[str] | None = None) -> set[str]:
    """Every key at any depth that is not allowed."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or not key_allowed(k):
                found.add(str(k))
            bad_keys(v, found)
    elif isinstance(value, list):
        for v in value:
            bad_keys(v, found)
    return found


def drop_keys(value: Any) -> Any:
    """A copy of ``value`` without the keys that are not allowed (at any depth)."""
    if isinstance(value, dict):
        return {k: drop_keys(v) for k, v in value.items() if isinstance(k, str) and key_allowed(k)}
    if isinstance(value, list):
        return [drop_keys(v) for v in value]
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

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        responses = dict(kwargs.pop("responses", None) or {})
        responses.setdefault(422, {"model": OpsProblem, "description": "Invalid request (field names only)"})
        responses.setdefault("4XX", {"model": OpsProblem, "description": "Refused"})
        super().__init__(path, endpoint, responses=responses, **kwargs)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        path = self.path

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
            bad = bad_keys(data)
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
                content=json.dumps(drop_keys(data), ensure_ascii=False, separators=(",", ":")).encode(),
                status_code=response.status_code,
                headers=headers,
                media_type="application/json",
            )

        return handler
