"""FastAPI entrypoint: ``uvicorn app.main:app``.

Every path is under ``/crm`` (CloudFront -> VPC origin -> ALB host + path rule -> this service; AA-CIS-Infra
``accounts/aa365/crm_app.tf``):

- ``/crm/health``, ``/crm/health/ready``: public, rate limited (``app.routers.health``);
- ``/crm/ci/deploy-gate``: the Deploy Dev workflow's GitHub OIDC token only (``app.routers.ci``);
- ``/crm/public/v1``: the public inquiry form, no token, rate limited (``app.routers.public``, AA-936);
- ``/crm/v1``, ``/crm/ops/v1``, ``/crm/staff/v1``: the three route classes, each behind its token-audience guard
  (``app.auth.route_guard``, root ADR 0006b decision 6).

When ``WORKER_ENABLED`` is on, the lifespan also runs the ``crm.job`` worker loop (``app.jobs.worker``) as one
background task of this process — never from a request path — and the backlog metrics sampler (``AA/Jobs/Crm``).

Observability (copied from AA-Booking, AA-871): JSON log lines on stdout, a request id per request, Sentry when
``SENTRY_DSN`` is set. :func:`build_app` sets logging and Sentry up once per process; :func:`create_app` only adds the
middleware, so tests that build their own app never touch either.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.audit_export.archive import AuditArchive, build_audit_archive
from app.auth import identity_config
from app.auth.ci import GithubDeployVerifier
from app.auth.identity_verifier import MultiIssuerVerifier
from app.db import Database
from app.documents.store import DocumentStore, MemoryDocumentStore, S3DocumentStore
from app.errors import ServiceError, service_error_handler
from app.jobs import handlers as job_handlers
from app.jobs.metrics import JobMetricsSampler
from app.jobs.worker import Worker
from app.members.directory import CognitoDirectory, MemoryDirectory, PartnerDirectory
from app.members.mail import Mailer, MemoryMailer, SesMailer
from app.observability import setup as setup_observability
from app.observability.request_id import RequestIdMiddleware, unhandled_error_handler
from app.ratelimit import FixedWindowLimiter
from app.routers import agency, audit, ci, health, invitations, ops, pipeline, public, staff, team, travellers
from app.routers import agency, audit, ci, health, lifecycle, ops, pipeline, public, staff, travellers
from app.settings import INSECURE_ENVIRONMENTS, Settings, load_settings
from app.vault import KeyProvider, build_key_provider

# On shutdown a job in flight gets this long to finish; then the worker task is cancelled and releases the job back to
# the queue at once (app.jobs.queue.release). ECS gives the container 30 s after SIGTERM (stopTimeout).
WORKER_STOP_TIMEOUT_S = 20.0

_UNSET = object()


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    verifier: MultiIssuerVerifier | None | object = _UNSET,
    deploy_verifier: GithubDeployVerifier | None = None,
    keys: KeyProvider | None | object = _UNSET,
    documents: DocumentStore | None | object = _UNSET,
    audit_archive: AuditArchive | None | object = _UNSET,
    directory: PartnerDirectory | None | object = _UNSET,
    mailer: Mailer | None | object = _UNSET,
) -> FastAPI:
    settings = settings or load_settings()
    db = db or Database(settings)
    identity = identity_config.identity_from_settings(settings)
    if verifier is _UNSET:
        verifier = identity_config.build_verifier(identity)
    deploy_verifier = deploy_verifier or GithubDeployVerifier.default()
    if keys is _UNSET:
        keys = build_key_provider(settings)  # no KMS call here: the client is created on first use
    if documents is _UNSET:
        documents = build_document_store(settings)
    if audit_archive is _UNSET:
        audit_archive = build_audit_archive(settings)  # no AWS call here: the client is created on first use
    if directory is _UNSET:
        directory = build_directory(settings)
    if mailer is _UNSET:
        mailer = build_mailer(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        sampler_task: asyncio.Task[None] | None = None
        prefetch_task = identity_config.start_jwks_prefetch(app.state.verifier, settings)
        if settings.worker_enabled and db.configured:
            worker = Worker(
                db.pool,
                job_handlers.build_handlers(
                    retention_dry_run=settings.job_retention_dry_run,
                    keys=app.state.keys,
                    audit_archive=app.state.audit_archive,
                    audit_retention_dry_run=settings.audit_retention_dry_run,
                    documents=app.state.documents,
                    lifecycle_dry_run=settings.lifecycle_dry_run,
                    dek_cache_ttl_s=settings.dek_cache_ttl_seconds,
                    sender=app.state.dispatch_sender,
                ),
                recurring=job_handlers.RECURRING,
                hooks=job_handlers.build_hooks(retention_utc_hour=settings.job_retention_utc_hour),
            )
            task = asyncio.create_task(worker.run_forever(stop), name="crm-job-worker")
            # Independent of the worker task: a stuck worker loop must still show in the metrics.
            sampler_task = asyncio.create_task(JobMetricsSampler(db.pool).run_forever(stop), name="crm-job-metrics")
        yield
        stop.set()
        await identity_config.stop_jwks_prefetch(prefetch_task)
        if sampler_task is not None:
            sampler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler_task
        if task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(task, timeout=WORKER_STOP_TIMEOUT_S)
        await app.state.db.close()

    app = FastAPI(
        title="AA-Partner API",
        lifespan=lifespan,
        # No public schema or docs on a public host; the contracts live in the root repo (docs/contracts).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.identity = identity
    app.state.verifier = verifier
    app.state.deploy_verifier = deploy_verifier
    app.state.keys = keys  # app.vault.KeyProvider, or None when KMS_DATA_KEY_ID is missing (vault calls answer 503)
    app.state.documents = documents  # app.documents.store, or None without DOCUMENTS_BUCKET (document routes: 503)
    app.state.audit_archive = audit_archive  # app.audit_export.archive, or None without the two audit buckets (AA-940)
    # AA-939: the partner pool admin calls and AA system mail (app.members); None = the routes answer 503.
    app.state.directory = directory
    app.state.mailer = mailer
    app.state.claim_limiter = FixedWindowLimiter(settings.rate_limit_claim_per_minute)
    # AA-942: the Booking line re-check a trip restore needs (CRM-19 plugs the B2B client in) and the outbound sender
    # behind the dispatcher gate (CRM-17); both None until those cards.
    app.state.booking_checker = None
    app.state.dispatch_sender = None
    app.state.public_limiter = FixedWindowLimiter(settings.rate_limit_public_per_minute)
    # AA-936: inquiry-form submissions, per client address per minute and per form per hour.
    app.state.intake_limiter = FixedWindowLimiter(settings.rate_limit_intake_per_minute)
    app.state.intake_form_limiter = FixedWindowLimiter(settings.rate_limit_intake_form_per_hour, window_s=3600.0)
    app.add_exception_handler(ServiceError, service_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.include_router(ci.router)
    app.include_router(public.router)
    app.include_router(agency.router)
    app.include_router(pipeline.router)
    app.include_router(travellers.router)
    app.include_router(audit.router)
    app.include_router(team.router)
    app.include_router(invitations.public_router)
    app.include_router(invitations.agency_router)
    app.include_router(lifecycle.router)
    app.include_router(ops.router)
    app.include_router(staff.router)
    return app


def build_document_store(settings: Settings) -> DocumentStore | None:
    """The documents bucket when configured; an in-memory store on test / local; else None (AA-937)."""
    if settings.documents_bucket:
        return S3DocumentStore(settings.documents_bucket)  # no AWS call here: the client is created on first use
    if settings.environment in INSECURE_ENVIRONMENTS:
        return MemoryDocumentStore()
    return None


def build_directory(settings: Settings) -> PartnerDirectory | None:
    """The partner pool when configured; an in-memory directory on test / local; else None (AA-939)."""
    if settings.cognito_partner_pool_id and settings.cognito_partner_issuer:
        return CognitoDirectory(settings.cognito_partner_pool_id, settings.cognito_partner_issuer)
    if settings.environment in INSECURE_ENVIRONMENTS:
        return MemoryDirectory(settings.cognito_partner_issuer or "https://cognito-idp.local.test/local_partner")
    return None


def build_mailer(settings: Settings) -> Mailer | None:
    """AA system mail through SES when configured; in-memory on test / local; else None (AA-939)."""
    if settings.ses_from_address and settings.ses_configuration_set:
        return SesMailer(settings.ses_from_address, settings.ses_configuration_set)
    if settings.environment in INSECURE_ENVIRONMENTS:
        return MemoryMailer()
    return None


def build_app() -> FastAPI:
    """The process's app (``uvicorn app.main:app``): settings from the environment, JSON logging and Sentry, then
    :func:`create_app`."""
    settings = load_settings()
    setup_observability(settings)
    return create_app(settings)


app = build_app()
