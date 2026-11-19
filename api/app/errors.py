"""Service-layer errors and their HTTP mapping (registered on the app in ``app.main``)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("app.request")


class ServiceError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"detail": self.code, "message": self.message, **self.extra}


class Forbidden(ServiceError):
    status_code = 403


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Gone(ServiceError):
    status_code = 410


class Invalid(ServiceError):
    status_code = 422


class Unavailable(ServiceError):
    status_code = 503


class PayloadTooLarge(ServiceError):
    """413 (AA-945: the mail history upload over its cap)."""

    status_code = 413


class RateLimited(ServiceError):
    """429 with ``Retry-After`` (seconds) from inside the service layer (the per-form intake quota)."""

    status_code = 429

    def __init__(self, code: str, retry_after_s: int, message: str = "") -> None:
        super().__init__(code, message)
        self.retry_after_s = retry_after_s


class DatabaseBusy(RateLimited):
    """503 with ``Retry-After`` (AA-969): no pooled connection came free in time (app.tenancy)."""

    status_code = 503


class UnavailableRetry(RateLimited):
    """503 with ``Retry-After`` (AA-1030): a service the route needs (Booking) did not answer; try again later."""

    status_code = 503


RETRY_AFTER_DB_BUSY_S = 2


async def service_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)  # noqa: S101 — registered for ServiceError only
    headers = {"Retry-After": str(exc.retry_after_s)} if isinstance(exc, RateLimited) else None
    return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)


def validation_refusal_fields(exc: RequestValidationError) -> str:
    """``type@loc`` of each refused field ("missing@body.seq"), at most 10; never the value that was sent."""
    out = []
    for err in exc.errors()[:10]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        out.append(f"{err.get('type', 'invalid')}@{loc}")
    return ",".join(out)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's own 422 answer (same body), plus one log line naming the route and the refused fields (AA-1144
    follow-up, S285 live check: a builder "Add note" answered 422 once and its cause was not in the logs)."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101 — registered for RequestValidationError only
    route = request.scope.get("route")
    log.info(
        "request_refused_invalid method=%s route=%s errors=%s",
        request.method, getattr(route, "path", None), validation_refusal_fields(exc),
    )  # fmt: skip
    return await request_validation_exception_handler(request, exc)
