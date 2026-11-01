"""FastAPI entrypoint: ``uvicorn app.main:app``.

Every path is under ``/crm`` (CloudFront -> VPC origin -> ALB host + path rule -> this service; AA-CIS-Infra
``accounts/aa365/crm_app.tf``):

- ``/crm/health``, ``/crm/health/ready``: public, rate limited (``app.routers.health``);
- ``/crm/ci/deploy-gate``: the Deploy Dev workflow's GitHub OIDC token only (``app.routers.ci``);
- ``/crm/public/v1``: the public inquiry form, no token, rate limited (``app.routers.public``, AA-936);
- ``/crm/v1``, ``/crm/ops/v1``, ``/crm/staff/v1``: the three route classes, each behind its token-audience guard
  (``app.auth.route_guard``, root ADR 0006b decision 6).

When ``WORKER_ENABLED`` is on, the lifespan also runs the ``crm.job`` worker loop (``app.jobs.worker``) as one
background task of this process — never from a request path — and the job sampler with its ops checks (S263).

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
from app.booking.checker import BookingChecker
from app.booking.client import BookingClient, BookingConfig
from app.booking.products import ProductCache
from app.catalog.client import CatalogConfig
from app.cis.activation import ActivationConfig
from app.cis.m2m import TokenProvider
from app.cis.membership_feed import FeedConfig
from app.db import Database
from app.documents.store import DocumentStore, MemoryDocumentStore, S3DocumentStore
from app.errors import ServiceError, service_error_handler
from app.esign.seal import build_sealer
from app.jobs import handlers as job_handlers
from app.jobs.metrics import JobMetricsSampler
from app.jobs.worker import Worker
from app.llm.gateway import build_gateway
from app.mail.providers.registry import build_providers
from app.mail.sender import MailSender
from app.mail.state import MailRuntime
from app.mail.store import IngestStore, MemoryIngestStore, S3IngestStore
from app.members.directory import CognitoDirectory, MemoryDirectory, PartnerDirectory
from app.members.mail import Mailer, MemoryMailer, SesMailer
from app.messaging.providers import build_providers as build_text_providers
from app.messaging.sender import ChannelSender, TextSender
from app.messaging.state import MessagingRuntime
from app.observability import setup as setup_observability
from app.observability.request_id import RequestIdMiddleware, unhandled_error_handler
from app.ratelimit import HOOK_MISSES_PER_MINUTE, FixedWindowLimiter
from app.routers import (
    aa,
    agency,
    ai,
    audit,
    builder,
    calendar,
    ci,
    client_pages,
    commissions,
    data,
    duty,
    esign,
    health,
    invitations,
    invoices,
    ledger,
    lifecycle,
    mail,
    messaging,
    onboarding,
    ops,
    ops_release,
    payments,
    pipeline,
    proposal_text,
    proposals,
    public,
    reports,
    staff,
    staff_ops,
    support_access,
    team,
    travellers,
)
from app.settings import INSECURE_ENVIRONMENTS, Settings, load_settings
from app.vault import KeyProvider, build_key_provider

# On shutdown a job in flight gets this long to finish; then the worker task is cancelled and releases the job back to
# the queue at once (app.jobs.queue.release). ECS gives the container 30 s after SIGTERM (stopTimeout).
WORKER_STOP_TIMEOUT_S = 20.0
# S263 review: the last flush of the ops alert events, after the worker (20 s + 5 s stays inside the 30 s stopTimeout).
OPS_FLUSH_ON_STOP_TIMEOUT_S = 5.0

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
    ingest_store: IngestStore | None | object = _UNSET,
    mail_runtime: MailRuntime | None | object = _UNSET,
    booking_client: BookingClient | None | object = _UNSET,
    messaging_runtime: MessagingRuntime | None | object = _UNSET,
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
    if ingest_store is _UNSET:
        ingest_store = build_ingest_store(settings)
    # AA-947: one token provider for every M2M call (one cached token per scope), shared by the CIS feed, the catalog
    # cache and the Booking client.
    tokens = build_tokens(settings)
    if booking_client is _UNSET:
        booking_client = build_booking_client(settings, tokens)
    if mail_runtime is _UNSET:
        mail_runtime = MailRuntime(
            keys=keys,  # type: ignore[arg-type]
            providers=build_providers(settings),
            mailer=mailer,  # type: ignore[arg-type]
            directory=directory,  # type: ignore[arg-type]
            web_origin=settings.invitation_origin,
        )
    if messaging_runtime is _UNSET:
        # AA-958: the agency's own Twilio (and the dev-only sandbox stub); no AA vendor account exists.
        messaging_runtime = MessagingRuntime(
            keys=keys,  # type: ignore[arg-type]
            providers=build_text_providers(settings.environment),
            hooks_origin=settings.messaging_hooks_origin(),
            mailer=mailer,  # type: ignore[arg-type]
            directory=directory,  # type: ignore[arg-type]
            web_origin=settings.invitation_origin,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        sampler_task: asyncio.Task[None] | None = None
        sampler: JobMetricsSampler | None = None
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
                    fx_provider=settings.fx_provider,
                    membership_feed=build_membership_feed(settings, tokens),
                    studio_activation=app.state.studio_activation,
                    booking_client=app.state.booking_client,
                    catalog=build_catalog(settings, tokens),
                    directory=app.state.directory,
                    mail_runtime=app.state.mail_runtime,
                    ingest_store=app.state.ingest_store,
                    ingest_domain=settings.crm_ingest_domain,
                    messaging_runtime=app.state.messaging_runtime,
                ),
                recurring=job_handlers.RECURRING,
                hooks=job_handlers.build_hooks(
                    retention_utc_hour=settings.job_retention_utc_hour,
                    ingest_store=app.state.ingest_store if settings.crm_ingest_domain else None,
                ),
            )
            task = asyncio.create_task(worker.run_forever(stop), name="crm-job-worker")
            # Independent of the worker task: a stuck worker loop must still show in the metrics.
            sampler = JobMetricsSampler(db.pool)
            sampler_task = asyncio.create_task(sampler.run_forever(stop), name="crm-job-metrics")
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
        if sampler is not None:
            # S263 review: the ops alert events (break-glass, ops release, refused membership event, ...) raised since
            # the last 5-minute tick, including by the job the worker just finished, go out before the pool closes.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(sampler.flush_events(), timeout=OPS_FLUSH_ON_STOP_TIMEOUT_S)
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
    # AA-942: the Booking line re-check a trip restore needs (CRM-19 plugs the B2B client in). AA-945 (CRM-17): the
    # e-mail sender behind the dispatcher gate (the agency's own mailbox; never AA's SES), the mail runtime (send-as
    # providers, system mailer for the mailbox alert, directory) and the ingest bucket (forward-in + history import).
    app.state.booking_client = booking_client  # AA-947 (CRM-19): booking.b2b.v1, None without BOOKING_API_URL
    app.state.booking_checker = BookingChecker(booking_client) if isinstance(booking_client, BookingClient) else None
    app.state.aa_products = ProductCache(booking_client) if isinstance(booking_client, BookingClient) else None
    app.state.mail_runtime = mail_runtime
    # AA-979 P2b: CIS activateInvitation for accepted Content studio invitations (None without CIS_API_URL /
    # M2M_CLIENT: the accept answers "accepted" and the crm_studio_activation job waits).
    app.state.studio_activation = build_studio_activation(settings, tokens)
    app.state.messaging_runtime = messaging_runtime
    # AA-958: one dispatcher sender, e-mail through the AA-945 mail sender, SMS / WhatsApp through the agency's Twilio.
    app.state.dispatch_sender = ChannelSender(
        mail=MailSender(mail_runtime) if isinstance(mail_runtime, MailRuntime) else None,
        text=TextSender(messaging_runtime) if isinstance(messaging_runtime, MessagingRuntime) else None,
    )
    app.state.ingest_store = ingest_store
    # AA-955 (CRM-25): the platform seal of signed envelopes (KMS alias/crm-<env>/seal; the HMAC stub on dev / test /
    # local without KMS_SEAL_KEY_ID; None on stg / prod without it: signing answers 503 esign_not_configured).
    app.state.sealer = build_sealer(settings)
    app.state.public_limiter = FixedWindowLimiter(settings.rate_limit_public_per_minute)
    # AA-958: the Twilio webhooks count only unknown hook ids per address (app.routers.public), never genuine callbacks.
    app.state.hook_miss_limiter = FixedWindowLimiter(HOOK_MISSES_PER_MINUTE)
    # AA-964 (CRM-33a): the one LLM gateway of the process (stub provider unless CRM_LLM_PROVIDER=bedrock).
    app.state.llm_gateway = build_gateway(settings, keys)
    # AA-936: inquiry-form submissions, per client address per minute and per form per hour.
    app.state.intake_limiter = FixedWindowLimiter(settings.rate_limit_intake_per_minute)
    app.state.intake_form_limiter = FixedWindowLimiter(settings.rate_limit_intake_form_per_hour, window_s=3600.0)
    # AA-953 follow-up: traveller portal writes made with a session cookie get their own per-address bucket, so a
    # family on one home address is not stopped by the intake ceiling (app.routers.client_pages).
    app.state.portal_write_limiter = FixedWindowLimiter(settings.rate_limit_portal_write_per_minute)
    # ... and a session cookie only lifts a write to that bucket once it proves valid: each such write holds a slot of
    # this one (the anonymous ceiling) until the session check passes, so forged or stale cookies stay at 5 a minute.
    app.state.portal_unproven_limiter = FixedWindowLimiter(settings.rate_limit_intake_per_minute)
    app.add_exception_handler(ServiceError, service_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.include_router(ci.router)
    app.include_router(public.router)
    app.include_router(client_pages.router)
    app.include_router(agency.router)
    app.include_router(pipeline.router)
    app.include_router(travellers.router)
    app.include_router(onboarding.router)
    app.include_router(audit.router)
    app.include_router(team.router)
    app.include_router(invitations.public_router)
    app.include_router(invitations.agency_router)
    app.include_router(lifecycle.router)
    app.include_router(data.router)
    app.include_router(ledger.router)
    app.include_router(aa.router)
    app.include_router(proposals.router)
    app.include_router(invoices.router)
    app.include_router(payments.router)
    app.include_router(esign.router)
    app.include_router(duty.router)
    app.include_router(calendar.router)
    app.include_router(reports.router)
    app.include_router(builder.router)
    app.include_router(proposal_text.router)
    app.include_router(mail.router)
    app.include_router(messaging.router)
    app.include_router(ai.router)
    app.include_router(ops.router)
    app.include_router(support_access.router)
    app.include_router(staff.router)
    app.include_router(staff_ops.router)
    app.include_router(ops_release.router)
    app.include_router(commissions.router)
    return app


def build_document_store(settings: Settings) -> DocumentStore | None:
    """The documents bucket when configured; an in-memory store on test / local; else None (AA-937)."""
    if settings.documents_bucket:
        return S3DocumentStore(settings.documents_bucket)  # no AWS call here: the client is created on first use
    if settings.environment in INSECURE_ENVIRONMENTS:
        return MemoryDocumentStore()
    return None


def build_tokens(settings: Settings) -> TokenProvider | None:
    """The CRM's aa-m2m client (AA-978), or None without M2M_CLIENT. No network call here."""
    credentials = settings.m2m_credentials
    return TokenProvider(credentials) if credentials is not None else None


def build_membership_feed(settings: Settings, tokens: TokenProvider | None = None) -> FeedConfig | None:
    """The CIS membership feed's endpoint and M2M token (AA-978); None when CIS_API_URL or M2M_CLIENT is empty (the
    pull then logs not_configured). No network call here: the first token is asked for on the first pull."""
    tokens = tokens or build_tokens(settings)
    if not settings.cis_api_url or tokens is None:
        return None
    return FeedConfig(base_url=settings.cis_api_url, tokens=tokens)


def build_studio_activation(settings: Settings, tokens: TokenProvider | None) -> ActivationConfig | None:
    """CIS tenant.membership.v1 activateInvitation (AA-979); None when CIS_API_URL or M2M_CLIENT is empty."""
    if not settings.cis_api_url or tokens is None:
        return None
    return ActivationConfig(base_url=settings.cis_api_url, tokens=tokens)


def build_catalog(settings: Settings, tokens: TokenProvider | None) -> CatalogConfig | None:
    """CIS catalog.http.v1 for the AA tour cache (AA-947); None when CIS_API_URL or M2M_CLIENT is empty."""
    if not settings.cis_api_url or tokens is None:
        return None
    return CatalogConfig(base_url=settings.cis_api_url, tokens=tokens)


def build_booking_client(settings: Settings, tokens: TokenProvider | None) -> BookingClient | None:
    """booking.b2b.v1 (AA-947); None when BOOKING_API_URL or M2M_CLIENT is empty (requests stay "AA pending")."""
    if not settings.booking_api_url or tokens is None:
        return None
    return BookingClient(BookingConfig(base_url=settings.booking_api_url, tokens=tokens))


def build_ingest_store(settings: Settings) -> IngestStore | None:
    """The ingest bucket when configured; an in-memory store on test / local; else None (AA-945)."""
    if settings.crm_ingest_bucket:
        return S3IngestStore(settings.crm_ingest_bucket)  # no AWS call here: the client is created on first use
    if settings.environment in INSECURE_ENVIRONMENTS:
        return MemoryIngestStore()
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
