"""Process configuration, read once from the environment.

Names (set by the ECS task definition in AA-CIS-Infra ``accounts/aa365/crm_app.tf``):

- ``DATABASE_URL``: DSN for role ``crm_app`` (Secrets Manager ``crm/dev/database-url``). Optional: without it the
  service starts and ``/crm/health/ready`` answers 503.
- ``DB_SSL``: ``require`` (default) or ``disable``. ``disable`` is only accepted when ``ENVIRONMENT`` is ``test`` or
  ``local`` (CI's Postgres service has no TLS).
- ``ENVIRONMENT``: ``dev`` (default), ``test`` or ``local``.
- ``GIT_SHA`` / ``APP_VERSION``: commit and release version the image was built from (Docker build args), echoed by
  ``/crm/health``.
- ``WORKER_ENABLED``: ``true`` / ``false``. Runs the ``crm.job`` worker loop inside the API process. Default: on when
  ``ENVIRONMENT`` is ``dev`` and ``DATABASE_URL`` is set, off otherwise.
- ``SENTRY_DSN`` / ``SENTRY_REQUIRED``: error tracking (``app.observability.sentry``), as in AA-Booking.
- ``JOB_RETENTION_DRY_RUN``: ``true`` (default) makes ``crm_job_retention`` count only (DM-7.2: the first runs of a
  TTL kind are DRY RUN on synthetic agencies). Turn it off once a dry-run result has been checked.
- ``JOB_RETENTION_UTC_HOUR``: the UTC hour (0-23) from which the day's ``crm_job_retention`` run is queued (default 19,
  02:00 in Vietnam, the hour AA-Booking's retention uses). The run is queued due-now at that hour, never ahead.
- ``LIFECYCLE_DRY_RUN``: ``true`` (default, AA-942) makes every hard delete of agency data count only: the daily
  ``crm_lifecycle`` run (submission redaction, LLM-call purge, trash purge, auto-archive) reports what it would do, and
  an owner's erasure or offboarding erase answers 409 ``deletion_dry_run`` with the counts. Same hour as the job
  retention. Turning it off is an ops decision recorded in the runbook.
- ``FX_PROVIDER`` (AA-944): where the daily ``crm_fx_fetch`` job gets platform FX rates: ``synthetic`` (the default on
  ``dev`` / ``test`` / ``local``: a fixed table, no network) or ``live`` (the default on ``stg`` / ``prod``:
  ExchangeRate-API open access + ECB, cross-checked with fawazahmed0; free sources, ``app.fx.fetch``).

Identity (root ADR 0006b decision 6, ``app.auth.identity_config``). Every value is optional and has **no default**:
this repo is public, so pool ids never live in code (DM-6.10); Terraform passes them from the pool resources. A route
class whose values are empty accepts no token at all (fail closed).

- ``COGNITO_WORKFORCE_ISSUER``: WORKFORCE pool issuer (``https://cognito-idp.<region>.amazonaws.com/<pool id>``).
- ``COGNITO_CIS_ADMIN_CLIENT_ID``: the ``cis-admin`` client (``/crm/ops/v1``, the CIS admin BFF forwards it).
- ``COGNITO_BREAKGLASS_CLIENT_ID``: the ``breakglass`` client. Accepted by **no** route class; known to the verifier
  only so that its tokens get a clear 403 instead of a 401 (the card's R test).
- ``COGNITO_CRM_STAFF_CLIENT_ID``: the ``crm-staff`` client (``/crm/staff/v1``, created by AA-939).
- ``COGNITO_PARTNER_ISSUER`` / ``COGNITO_PARTNER_WEB_CLIENT_ID``: the partner pool and the AA-Partner web client
  (``/crm/v1``, created by AA-939).
- ``COGNITO_EXTERNAL_ISSUER`` / ``COGNITO_CIS_PORTAL_CLIENT_ID``: **retired** (AA-979 P2b). They configured the Dev-only
  EXTERNAL ``cis-portal`` bridge onto ``/crm/v1``; the API no longer reads them (a task that still has them starts and
  ignores them, so the code can ship before the Terraform env change).

Field encryption (AA-934, design v3 §4.1; ``app.vault``):

- ``KEY_PROVIDER``: ``kms`` or ``local``. Default ``kms`` when ``ENVIRONMENT`` is ``dev``/``stg``/``prod``, ``local``
  when it is ``test``/``local``. ``local`` (an in-process AES keyring instead of KMS) is refused outside
  ``test``/``local``.
- ``KMS_DATA_KEY_ID``: the KMS key that wraps the per-tenant DEKs, as an alias (``alias/crm-<env>/data``, set by
  Terraform), a key ARN or a key id. Optional: without it (and with ``KEY_PROVIDER=kms``) the service starts, the
  readiness body says ``keys: not_configured`` and every encrypt / decrypt answers 503 ``key_provider_not_configured``.
- ``LOCAL_KEK_B64``: ``local`` only; base64 of a 32-byte KEK. Empty = a random KEK per process (data written by an
  earlier process is then unreadable, which is fine for tests).
- ``DEK_CACHE_TTL_SECONDS``: how long an unwrapped DEK stays in the process cache (default 300, the design's "about
  5 minutes"; 0 turns the cache off; at most 900).

Rate limits (ADR 0010 decision 2: CloudFront → VPC origin bypasses API Gateway throttling):

- ``RATE_LIMIT_PUBLIC_PER_MINUTE``: requests per client address per minute on the unauthenticated routes (default
  120; 0 turns the limit off).
- ``RATE_LIMIT_XFF_HOPS``: **fallback only**. The limiter keys on ``CloudFront-Viewer-Address`` (set by CloudFront,
  not by the viewer); only when that header is missing does it use the ``X-Forwarded-For`` entry this many from the
  right (default 2: the viewer address CloudFront appended, then the address the ALB appended). See ``app.ratelimit``.
- ``RATE_LIMIT_INTAKE_PER_MINUTE``: inquiry-form submissions per client address per minute (default 5; 0 = off).
- ``RATE_LIMIT_INTAKE_FORM_PER_HOUR``: submissions per form per hour, whatever the address (default 200; 0 = off).

Public intake (AA-936, card CRM-08; ``app.intake.public``):

- ``INTAKE_PAGE_ORIGINS``: comma list of the https origins whose pages may POST a submission (the client host,
  ``https://go-dev.…``, set by Terraform). A form's own ``allowed_origins`` are accepted too. Empty on ``dev`` /
  ``stg`` / ``prod`` = every submission answers 503 ``intake_not_configured`` (fail closed); on ``test`` / ``local``
  empty means "no Origin check".

Documents (AA-937, card CRM-09; ``app.documents``):

- ``DOCUMENTS_BUCKET``: the ``aa-crm-<env>-documents-*`` bucket (set by Terraform). Objects are encrypted by the app
  with the tenant ``docs`` DEK before the PUT; the bucket adds SSE-KMS. Empty on ``dev`` / ``stg`` / ``prod`` = the
  document routes answer 503 ``documents_not_configured``; on ``test`` / ``local`` empty = an in-memory store.

Audit export (AA-940, card CRM-12; ``app.audit_export``):

- ``AUDIT_BUCKET_COMPLIANCE`` / ``AUDIT_BUCKET_GOVERNANCE``: the two Object Lock audit buckets
  (``aa-crm-<env>-audit-c-*`` = the 12-month tier, ``aa-crm-<env>-audit-g-*`` = the 7-year tier; set by Terraform).
  Both or neither. Empty on ``dev`` / ``stg`` / ``prod`` = the hourly export and the daily verification skip (the
  export lag metric grows and its alarm says so); on ``test`` / ``local`` empty = an in-memory archive.
- ``AUDIT_RETENTION_DRY_RUN``: ``true`` (default) makes ``crm_audit_retention`` report which audit partitions it would
  drop without dropping them (DM-7.2). Partitions are still created and verification still runs.

Team and roles (AA-939, card CRM-11; ``app.members``):

- ``COGNITO_PARTNER_POOL_ID``: the partner pool (``<region>_<id>``) the invitation claim step creates accounts in
  (``AdminCreateUser`` / ``ListUsers``) and member removal signs out of (``AdminUserGlobalSignOut``). Must be the pool
  of ``COGNITO_PARTNER_ISSUER``. Empty on ``dev`` / ``stg`` / ``prod`` = the invitation routes answer 503
  ``directory_not_configured``; on ``test`` / ``local`` an in-memory directory.
- ``PARTNER_WEB_ORIGIN``: the https origin of the partner host (the join link in the invitation mail). Required for
  invitations outside test / local (503 ``invitations_not_configured``).
- ``SES_FROM_ADDRESS`` / ``SES_CONFIGURATION_SET``: AA system mail (invitations, owner recovery notices) through
  AA's SES as that sender and configuration set. Empty outside test / local = 503 ``mail_not_configured``.
- ``INVITATION_TTL_HOURS``: how long an invitation link works (default 168 = 7 days, 1 to 720).
- ``RATE_LIMIT_CLAIM_PER_MINUTE``: invitation claims per client address per minute (default 10; 0 = off).

F3 PII scrubber (AA-938, card CRM-10; ``app.privacy``):

- ``SCRUB_GUARDRAIL_ID`` / ``SCRUB_GUARDRAIL_VERSION``: the Bedrock guardrail of pass 3 (AA-CIS-Infra
  ``crm_scrub.tf``, acc2). Both or neither; empty = every scrub fails closed (no LLM call can be made).
- ``SCRUB_GUARDRAIL_REGION``: its region (default ``us-west-1``).
- ``SCRUB_DETECTOR_TIMEOUT_MS``: how long pass 3 may take (default 3000, 200..10000); a timeout refuses the call
  (``scrub_timeout``, the draft says "reply by hand").

CIS membership feed (AA-978, ``app.cis``):

- ``CIS_API_URL``: the origin of the CIS public API (``https://api-cis.…``, no path, no trailing slash; Terraform
  ``var.crm_cis_api_url``). The worker pulls ``<CIS_API_URL>/tenant/membership/v1/events``.
- ``M2M_CLIENT``: the CRM's aa-m2m client as the JSON of its secret ``{client_id, client_secret, token_url, scopes}``
  (``aa-crm/<env>/m2m-booking-client``). ``crm_membership_pull`` asks for scope ``cis.tenant/read`` with it.
  Either empty = the job logs ``not_configured`` and does nothing. A value that is not that JSON refuses to start
  (the value is never echoed).

AA supply (AA-947, card CRM-19; ``app.booking``, ``app.catalog``):

- ``BOOKING_API_URL``: the origin of the Booking public API (``https://api-booking.…``, no path; Terraform
  ``var.crm_booking_api_url``). The CRM calls ``<BOOKING_API_URL>/booking/b2b/v1/...`` with the same ``M2M_CLIENT``
  (one scope per call: ``booking.b2b/read``, ``/quote``, ``/hold``, ``/book``). Empty = ``crm_booking_sync`` logs
  ``not_configured``, AA requests stay "AA pending" and the product look-up answers 503 ``booking_not_configured``.
- The catalog cache (``crm_catalog_sync``) reads ``<CIS_API_URL>/catalog/v1`` with scope ``cis.catalog/read``.

Mail (AA-945, card CRM-17; ``app.mail``; AA-CIS-Infra ``crm_mail.tf``):

- ``CRM_INGEST_BUCKET``: the ``aa-crm-<env>-ingest-*`` bucket the forward-in receipt rule writes ``raw/`` to (and the
  history import's ``imports/``). Empty on ``dev`` / ``stg`` / ``prod`` = no ingest scan, and the import route answers
  503 ``mail_ingest_not_configured``; on ``test`` / ``local`` empty = an in-memory store.
- ``CRM_INGEST_DOMAIN``: the forward-in domain (``crm-in.<zone>``); an agency's address is ``<local_part>@`` it.
- ``MAIL_OAUTH_REDIRECT_BASE``: the https origin of the partner host; the OAuth redirect URI is
  ``<it>/connect/mail/callback`` (the web route).
- ``MAIL_OAUTH`` (Secrets Manager ``crm/<env>/mail-oauth``, JSON ``google_client_id``, ``google_client_secret``,
  ``ms_client_id``, ``ms_client_secret``): AA's own OAuth clients for the agencies' Gmail / Microsoft 365 send-as.
  Without a provider's pair its connect answers 503 ``mail_oauth_not_configured``; SMTP needs none.

Traveller portal (AA-946, card CRM-18; ``app.portal``):

- ``PORTAL_OTP_DEV_ECHO``: ``true`` returns the portal sign-in / signing code in the API answer (``dev_code``) instead
  of only mailing it through the agency's mailbox, so the synthetic Dev agencies (no mailbox) can sign in. Default
  ``false``; refused at start-up unless ``ENVIRONMENT`` is ``dev``, ``test`` or ``local``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.cis.m2m import CredentialsError, M2mCredentials, parse_credentials

SERVICE_NAME = "aa-crm-api"

SSL_MODES = frozenset({"require", "disable"})
INSECURE_ENVIRONMENTS = frozenset({"test", "local"})
# Environments where Dev-only aids are allowed (PORTAL_OTP_DEV_ECHO).
DEV_ONLY_ENVIRONMENTS = frozenset({"dev", "test", "local"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

DEFAULT_RATE_LIMIT_PER_MINUTE = 120
DEFAULT_XFF_HOPS = 2
DEFAULT_RETENTION_UTC_HOUR = 19
DEFAULT_INTAKE_PER_MINUTE = 5
DEFAULT_INTAKE_FORM_PER_HOUR = 200
DEFAULT_INVITATION_TTL_HOURS = 168
MAX_INVITATION_TTL_HOURS = 720
DEFAULT_CLAIM_PER_MINUTE = 10
_POOL_ID = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]_[A-Za-z0-9]{1,55}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[a-z0-9.-]{1,189}\.[a-z]{2,}$")
KEY_PROVIDERS = frozenset({"kms", "local"})
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
DEFAULT_DEK_CACHE_TTL_S = 300
MAX_DEK_CACHE_TTL_S = 900
_KMS_KEY_PREFIXES = ("alias/", "arn:aws:kms:")
DEFAULT_SCRUB_TIMEOUT_MS = 3000
_GUARDRAIL_ID = re.compile(r"^[a-z0-9]{1,64}$")
_GUARDRAIL_VERSION = re.compile(r"^(DRAFT|[1-9][0-9]{0,7})$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")


FX_PROVIDERS = frozenset({"synthetic", "live"})


class SettingsError(ValueError):
    """The environment holds an invalid or unsafe value."""


@dataclass(frozen=True)
class Settings:
    database_url: str | None = field(default=None, repr=False)
    db_ssl: str = "require"
    environment: str = "dev"
    git_sha: str = "unknown"
    app_version: str = "unknown"
    worker_enabled: bool = False
    cognito_workforce_issuer: str = ""
    cognito_cis_admin_client_id: str = ""
    cognito_breakglass_client_id: str = ""
    cognito_crm_staff_client_id: str = ""
    cognito_partner_issuer: str = ""
    cognito_partner_web_client_id: str = ""
    rate_limit_public_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    rate_limit_xff_hops: int = DEFAULT_XFF_HOPS
    sentry_dsn: str = field(default="", repr=False)
    sentry_required: bool = False
    job_retention_dry_run: bool = True
    job_retention_utc_hour: int = DEFAULT_RETENTION_UTC_HOUR
    key_provider: str = "kms"
    kms_data_key_id: str = ""
    local_kek_b64: str = field(default="", repr=False)
    dek_cache_ttl_seconds: int = DEFAULT_DEK_CACHE_TTL_S
    rate_limit_intake_per_minute: int = DEFAULT_INTAKE_PER_MINUTE
    rate_limit_intake_form_per_hour: int = DEFAULT_INTAKE_FORM_PER_HOUR
    intake_page_origins: tuple[str, ...] = ()
    documents_bucket: str = ""
    audit_bucket_compliance: str = ""
    audit_bucket_governance: str = ""
    audit_retention_dry_run: bool = True
    cognito_partner_pool_id: str = ""
    partner_web_origin: str = ""
    ses_from_address: str = ""
    ses_configuration_set: str = ""
    invitation_ttl_hours: int = DEFAULT_INVITATION_TTL_HOURS
    rate_limit_claim_per_minute: int = DEFAULT_CLAIM_PER_MINUTE
    scrub_guardrail_id: str = ""
    scrub_guardrail_version: str = ""
    scrub_guardrail_region: str = "us-west-1"
    scrub_detector_timeout_ms: int = DEFAULT_SCRUB_TIMEOUT_MS
    lifecycle_dry_run: bool = True
    fx_provider: str = "synthetic"
    cis_api_url: str = ""
    booking_api_url: str = ""
    m2m_client: str = field(default="", repr=False)
    crm_ingest_bucket: str = ""
    crm_ingest_domain: str = ""
    mail_oauth_redirect_base: str = ""
    # (key, value) pairs of MAIL_OAUTH (a tuple keeps Settings hashable); read through mail_oauth_client().
    mail_oauth: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    portal_otp_dev_echo: bool = False

    def __post_init__(self) -> None:
        if self.environment not in {"dev", "stg", "prod"} | INSECURE_ENVIRONMENTS:
            raise SettingsError(f"ENVIRONMENT must be dev, stg, prod, test or local, got {self.environment!r}")
        if self.fx_provider not in FX_PROVIDERS:
            raise SettingsError(f"FX_PROVIDER must be one of {sorted(FX_PROVIDERS)}, got {self.fx_provider!r}")
        if self.db_ssl not in SSL_MODES:
            raise SettingsError(f"DB_SSL must be one of {sorted(SSL_MODES)}, got {self.db_ssl!r}")
        if self.db_ssl == "disable" and self.environment not in INSECURE_ENVIRONMENTS:
            raise SettingsError(
                f"DB_SSL=disable is only allowed when ENVIRONMENT is one of {sorted(INSECURE_ENVIRONMENTS)}"
            )
        for name, issuer in (
            ("COGNITO_WORKFORCE_ISSUER", self.cognito_workforce_issuer),
            ("COGNITO_PARTNER_ISSUER", self.cognito_partner_issuer),
        ):
            if issuer and (not issuer.startswith("https://") or issuer.endswith("/")):
                raise SettingsError(f"{name} must be an https:// URL without a trailing slash, got {issuer!r}")
        clients = {
            "COGNITO_CIS_ADMIN_CLIENT_ID": self.cognito_cis_admin_client_id,
            "COGNITO_BREAKGLASS_CLIENT_ID": self.cognito_breakglass_client_id,
            "COGNITO_CRM_STAFF_CLIENT_ID": self.cognito_crm_staff_client_id,
            "COGNITO_PARTNER_WEB_CLIENT_ID": self.cognito_partner_web_client_id,
        }
        for name, client_id in clients.items():
            if client_id and not client_id.isalnum():
                raise SettingsError(f"{name} must be a Cognito app client id (letters and digits), got {client_id!r}")
        workforce = [v for k, v in clients.items() if k in _WORKFORCE_CLIENTS and v]
        if len(workforce) != len(set(workforce)):
            # One id in two classes would let a token of one class pass the other's audience check.
            raise SettingsError("the WORKFORCE client ids (cis-admin, breakglass, crm-staff) must differ")
        if self.portal_otp_dev_echo and self.environment not in DEV_ONLY_ENVIRONMENTS:
            raise SettingsError("PORTAL_OTP_DEV_ECHO is a Dev-only aid (AA-946): never on stg or prod")
        if self.rate_limit_public_per_minute < 0:
            raise SettingsError("RATE_LIMIT_PUBLIC_PER_MINUTE must be >= 0")
        if not 1 <= self.rate_limit_xff_hops <= 5:
            raise SettingsError("RATE_LIMIT_XFF_HOPS must be between 1 and 5")
        if not 0 <= self.job_retention_utc_hour <= 23:
            raise SettingsError("JOB_RETENTION_UTC_HOUR must be between 0 and 23")
        if self.sentry_dsn and not self.sentry_dsn.startswith("https://"):
            raise SettingsError("SENTRY_DSN must be an https:// URL")  # the value is never echoed
        if self.sentry_required and not self.sentry_dsn:
            raise SettingsError("SENTRY_REQUIRED is on but SENTRY_DSN is empty")
        if self.documents_bucket and not _BUCKET.match(self.documents_bucket):
            raise SettingsError(f"DOCUMENTS_BUCKET must be an S3 bucket name, got {self.documents_bucket!r}")
        for name, value in (
            ("AUDIT_BUCKET_COMPLIANCE", self.audit_bucket_compliance),
            ("AUDIT_BUCKET_GOVERNANCE", self.audit_bucket_governance),
        ):
            if value and not _BUCKET.match(value):
                raise SettingsError(f"{name} must be an S3 bucket name, got {value!r}")
        if bool(self.audit_bucket_compliance) != bool(self.audit_bucket_governance):
            raise SettingsError("AUDIT_BUCKET_COMPLIANCE and AUDIT_BUCKET_GOVERNANCE are set together (both tiers)")
        if self.audit_bucket_compliance and self.audit_bucket_compliance == self.audit_bucket_governance:
            raise SettingsError("the two audit buckets must differ")
        if bool(self.scrub_guardrail_id) != bool(self.scrub_guardrail_version):
            raise SettingsError("SCRUB_GUARDRAIL_ID and SCRUB_GUARDRAIL_VERSION are set together or not at all")
        if self.scrub_guardrail_id and not _GUARDRAIL_ID.match(self.scrub_guardrail_id):
            raise SettingsError("SCRUB_GUARDRAIL_ID must be a guardrail id (lower-case letters and digits)")
        if self.scrub_guardrail_version and not _GUARDRAIL_VERSION.match(self.scrub_guardrail_version):
            raise SettingsError("SCRUB_GUARDRAIL_VERSION must be a published version number or DRAFT")
        if not _REGION.match(self.scrub_guardrail_region):
            raise SettingsError(f"SCRUB_GUARDRAIL_REGION must be an AWS region, got {self.scrub_guardrail_region!r}")
        if not 200 <= self.scrub_detector_timeout_ms <= 10_000:
            raise SettingsError("SCRUB_DETECTOR_TIMEOUT_MS must be between 200 and 10000")
        if self.key_provider not in KEY_PROVIDERS:
            raise SettingsError(f"KEY_PROVIDER must be one of {sorted(KEY_PROVIDERS)}, got {self.key_provider!r}")
        if self.key_provider == "local" and self.environment not in INSECURE_ENVIRONMENTS:
            raise SettingsError(
                f"KEY_PROVIDER=local is only allowed when ENVIRONMENT is one of {sorted(INSECURE_ENVIRONMENTS)}"
            )
        if self.local_kek_b64 and self.key_provider != "local":
            raise SettingsError("LOCAL_KEK_B64 is only read with KEY_PROVIDER=local")
        if self.kms_data_key_id and not (
            self.kms_data_key_id.startswith(_KMS_KEY_PREFIXES) or _looks_like_key_id(self.kms_data_key_id)
        ):
            raise SettingsError("KMS_DATA_KEY_ID must be an alias/..., a KMS key ARN or a key id")
        if not 0 <= self.dek_cache_ttl_seconds <= MAX_DEK_CACHE_TTL_S:
            raise SettingsError(f"DEK_CACHE_TTL_SECONDS must be between 0 and {MAX_DEK_CACHE_TTL_S}")
        if self.rate_limit_intake_per_minute < 0 or self.rate_limit_intake_form_per_hour < 0:
            raise SettingsError("RATE_LIMIT_INTAKE_PER_MINUTE / RATE_LIMIT_INTAKE_FORM_PER_HOUR must be >= 0")
        if self.cognito_partner_pool_id:
            if not _POOL_ID.match(self.cognito_partner_pool_id):
                raise SettingsError("COGNITO_PARTNER_POOL_ID must be a Cognito user pool id (<region>_<id>)")
            if not self.cognito_partner_issuer.endswith(f"/{self.cognito_partner_pool_id}"):
                raise SettingsError("COGNITO_PARTNER_POOL_ID must be the pool of COGNITO_PARTNER_ISSUER")
        if self.partner_web_origin and not _ORIGIN.match(self.partner_web_origin):
            raise SettingsError(f"PARTNER_WEB_ORIGIN must be an https origin, got {self.partner_web_origin!r}")
        if self.ses_from_address and not _EMAIL.match(self.ses_from_address):
            raise SettingsError("SES_FROM_ADDRESS must be an e-mail address")
        if bool(self.ses_from_address) != bool(self.ses_configuration_set):
            raise SettingsError("SES_FROM_ADDRESS and SES_CONFIGURATION_SET are set together")
        if not 1 <= self.invitation_ttl_hours <= MAX_INVITATION_TTL_HOURS:
            raise SettingsError(f"INVITATION_TTL_HOURS must be between 1 and {MAX_INVITATION_TTL_HOURS}")
        if self.rate_limit_claim_per_minute < 0:
            raise SettingsError("RATE_LIMIT_CLAIM_PER_MINUTE must be >= 0")
        for origin in self.intake_page_origins:
            if not _ORIGIN.match(origin):
                raise SettingsError(f"INTAKE_PAGE_ORIGINS entries must be https origins, got {origin!r}")
        insecure = self.environment in INSECURE_ENVIRONMENTS
        if self.cis_api_url and not (
            _ORIGIN.match(self.cis_api_url) or (insecure and _HTTP_ORIGIN.match(self.cis_api_url))
        ):
            raise SettingsError(f"CIS_API_URL must be an https origin without a path, got {self.cis_api_url!r}")
        if self.booking_api_url and not (
            _ORIGIN.match(self.booking_api_url) or (insecure and _HTTP_ORIGIN.match(self.booking_api_url))
        ):
            raise SettingsError(f"BOOKING_API_URL must be an https origin without a path, got {self.booking_api_url!r}")
        if self.m2m_client:
            try:
                parse_credentials(self.m2m_client, allow_http=insecure)
            except CredentialsError as exc:
                raise SettingsError(str(exc)) from None  # names the problem, never the value
        if self.crm_ingest_bucket and not _BUCKET.match(self.crm_ingest_bucket):
            raise SettingsError(f"CRM_INGEST_BUCKET must be an S3 bucket name, got {self.crm_ingest_bucket!r}")
        if self.crm_ingest_domain and not _DOMAIN.match(self.crm_ingest_domain):
            raise SettingsError(f"CRM_INGEST_DOMAIN must be a domain name, got {self.crm_ingest_domain!r}")
        if self.mail_oauth_redirect_base and not _ORIGIN.match(self.mail_oauth_redirect_base):
            raise SettingsError("MAIL_OAUTH_REDIRECT_BASE must be an https origin")
        keys = {k for k, _ in self.mail_oauth}
        if keys - MAIL_OAUTH_KEYS or not all(isinstance(v, str) for _, v in self.mail_oauth):
            # values are never echoed
            raise SettingsError(f"MAIL_OAUTH holds only string values of {sorted(MAIL_OAUTH_KEYS)}")

    @property
    def intake_origin_check(self) -> bool:
        """Whether submissions must carry an allowed Origin (always outside test / local)."""
        return bool(self.intake_page_origins) or self.environment not in INSECURE_ENVIRONMENTS

    @property
    def invitation_origin(self) -> str:
        """The join link's origin: PARTNER_WEB_ORIGIN, or a local placeholder on test / local."""
        if self.partner_web_origin:
            return self.partner_web_origin
        return "https://partner.local.test" if self.environment in INSECURE_ENVIRONMENTS else ""

    def mail_oauth_client(self, provider: str) -> tuple[str, str] | None:
        """(client id, client secret) of AA's OAuth app for ``google`` / ``microsoft``, or None when not set."""
        prefix = {"google": "google", "microsoft": "ms"}.get(provider)
        values = dict(self.mail_oauth)
        client_id, secret = values.get(f"{prefix}_client_id"), values.get(f"{prefix}_client_secret")
        return (client_id, secret) if prefix and client_id and secret else None

    @property
    def m2m_credentials(self) -> M2mCredentials | None:
        """The CRM's aa-m2m client (AA-978), or None when ``M2M_CLIENT`` is empty."""
        if not self.m2m_client:
            return None
        return parse_credentials(self.m2m_client, allow_http=self.environment in INSECURE_ENVIRONMENTS)

    @property
    def keys_state(self) -> str:
        """``kms`` / ``local`` when field encryption can run, else ``not_configured`` (readiness body)."""
        if self.key_provider == "kms" and not self.kms_data_key_id:
            return "not_configured"
        return self.key_provider


_ORIGIN = re.compile(r"^https://[a-z0-9.-]+(:[0-9]{1,5})?$")
_HTTP_ORIGIN = re.compile(r"^http://[a-z0-9.-]+(:[0-9]{1,5})?$")  # test / local only (CIS_API_URL)
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
MAIL_OAUTH_KEYS = frozenset({"google_client_id", "google_client_secret", "ms_client_id", "ms_client_secret"})


def _mail_oauth(raw: str | None) -> tuple[tuple[str, str], ...]:
    """``MAIL_OAUTH``: the secret's JSON object; empty values dropped. A value that is not JSON fails closed."""
    if raw is None or not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except ValueError:
        raise SettingsError("MAIL_OAUTH must be a JSON object") from None
    if not isinstance(value, dict):
        raise SettingsError("MAIL_OAUTH must be a JSON object")
    return tuple(sorted((str(k), v.strip() if isinstance(v, str) else v) for k, v in value.items() if v))


def _origins(raw: str | None) -> tuple[str, ...]:
    return tuple(o.strip().lower().rstrip("/") for o in (raw or "").split(",") if o.strip())


def _looks_like_key_id(value: str) -> bool:
    parts = value.split("-")
    return [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(c in "0123456789abcdef-" for c in value)


_WORKFORCE_CLIENTS = frozenset(
    {"COGNITO_CIS_ADMIN_CLIENT_ID", "COGNITO_BREAKGLASS_CLIENT_ID", "COGNITO_CRM_STAFF_CLIENT_ID"}
)


def _flag(raw: str | None, default: bool, name: str) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise SettingsError(f"{name} must be true or false, got {raw!r}")


def _int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from None


def _text(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    database_url = env.get("DATABASE_URL") or None
    environment = (env.get("ENVIRONMENT") or "dev").strip().lower()
    default_provider = "local" if environment in INSECURE_ENVIRONMENTS else "kms"
    return Settings(
        database_url=database_url,
        db_ssl=(env.get("DB_SSL") or "require").strip().lower(),
        environment=environment,
        git_sha=(env.get("GIT_SHA") or "unknown").strip(),
        app_version=(env.get("APP_VERSION") or "unknown").strip(),
        worker_enabled=_flag(
            env.get("WORKER_ENABLED"), environment == "dev" and database_url is not None, "WORKER_ENABLED"
        ),
        cognito_workforce_issuer=_text(env, "COGNITO_WORKFORCE_ISSUER"),
        cognito_cis_admin_client_id=_text(env, "COGNITO_CIS_ADMIN_CLIENT_ID"),
        cognito_breakglass_client_id=_text(env, "COGNITO_BREAKGLASS_CLIENT_ID"),
        cognito_crm_staff_client_id=_text(env, "COGNITO_CRM_STAFF_CLIENT_ID"),
        cognito_partner_issuer=_text(env, "COGNITO_PARTNER_ISSUER"),
        cognito_partner_web_client_id=_text(env, "COGNITO_PARTNER_WEB_CLIENT_ID"),
        rate_limit_public_per_minute=_int(
            env.get("RATE_LIMIT_PUBLIC_PER_MINUTE"), DEFAULT_RATE_LIMIT_PER_MINUTE, "RATE_LIMIT_PUBLIC_PER_MINUTE"
        ),
        rate_limit_xff_hops=_int(env.get("RATE_LIMIT_XFF_HOPS"), DEFAULT_XFF_HOPS, "RATE_LIMIT_XFF_HOPS"),
        sentry_dsn=_text(env, "SENTRY_DSN"),
        sentry_required=_flag(env.get("SENTRY_REQUIRED"), False, "SENTRY_REQUIRED"),
        job_retention_dry_run=_flag(env.get("JOB_RETENTION_DRY_RUN"), True, "JOB_RETENTION_DRY_RUN"),
        job_retention_utc_hour=_int(
            env.get("JOB_RETENTION_UTC_HOUR"), DEFAULT_RETENTION_UTC_HOUR, "JOB_RETENTION_UTC_HOUR"
        ),
        key_provider=(_text(env, "KEY_PROVIDER") or default_provider).lower(),
        kms_data_key_id=_text(env, "KMS_DATA_KEY_ID"),
        local_kek_b64=_text(env, "LOCAL_KEK_B64"),
        dek_cache_ttl_seconds=_int(env.get("DEK_CACHE_TTL_SECONDS"), DEFAULT_DEK_CACHE_TTL_S, "DEK_CACHE_TTL_SECONDS"),
        rate_limit_intake_per_minute=_int(
            env.get("RATE_LIMIT_INTAKE_PER_MINUTE"), DEFAULT_INTAKE_PER_MINUTE, "RATE_LIMIT_INTAKE_PER_MINUTE"
        ),
        rate_limit_intake_form_per_hour=_int(
            env.get("RATE_LIMIT_INTAKE_FORM_PER_HOUR"), DEFAULT_INTAKE_FORM_PER_HOUR, "RATE_LIMIT_INTAKE_FORM_PER_HOUR"
        ),
        intake_page_origins=_origins(env.get("INTAKE_PAGE_ORIGINS")),
        documents_bucket=_text(env, "DOCUMENTS_BUCKET"),
        audit_bucket_compliance=_text(env, "AUDIT_BUCKET_COMPLIANCE"),
        audit_bucket_governance=_text(env, "AUDIT_BUCKET_GOVERNANCE"),
        audit_retention_dry_run=_flag(env.get("AUDIT_RETENTION_DRY_RUN"), True, "AUDIT_RETENTION_DRY_RUN"),
        cognito_partner_pool_id=_text(env, "COGNITO_PARTNER_POOL_ID"),
        partner_web_origin=_text(env, "PARTNER_WEB_ORIGIN").lower().rstrip("/"),
        ses_from_address=_text(env, "SES_FROM_ADDRESS").lower(),
        ses_configuration_set=_text(env, "SES_CONFIGURATION_SET"),
        invitation_ttl_hours=_int(
            env.get("INVITATION_TTL_HOURS"), DEFAULT_INVITATION_TTL_HOURS, "INVITATION_TTL_HOURS"
        ),
        rate_limit_claim_per_minute=_int(
            env.get("RATE_LIMIT_CLAIM_PER_MINUTE"), DEFAULT_CLAIM_PER_MINUTE, "RATE_LIMIT_CLAIM_PER_MINUTE"
        ),
        scrub_guardrail_id=_text(env, "SCRUB_GUARDRAIL_ID"),
        scrub_guardrail_version=_text(env, "SCRUB_GUARDRAIL_VERSION"),
        scrub_guardrail_region=_text(env, "SCRUB_GUARDRAIL_REGION") or "us-west-1",
        scrub_detector_timeout_ms=_int(
            env.get("SCRUB_DETECTOR_TIMEOUT_MS"), DEFAULT_SCRUB_TIMEOUT_MS, "SCRUB_DETECTOR_TIMEOUT_MS"
        ),
        lifecycle_dry_run=_flag(env.get("LIFECYCLE_DRY_RUN"), True, "LIFECYCLE_DRY_RUN"),
        fx_provider=(env.get("FX_PROVIDER") or ("live" if environment in {"stg", "prod"} else "synthetic"))
        .strip()
        .lower(),
        cis_api_url=_text(env, "CIS_API_URL").lower().rstrip("/"),
        booking_api_url=_text(env, "BOOKING_API_URL").lower().rstrip("/"),
        m2m_client=_text(env, "M2M_CLIENT"),
        crm_ingest_bucket=_text(env, "CRM_INGEST_BUCKET"),
        crm_ingest_domain=_text(env, "CRM_INGEST_DOMAIN").lower(),
        mail_oauth_redirect_base=_text(env, "MAIL_OAUTH_REDIRECT_BASE").lower().rstrip("/"),
        mail_oauth=_mail_oauth(env.get("MAIL_OAUTH")),
        portal_otp_dev_echo=_flag(env.get("PORTAL_OTP_DEV_ECHO"), False, "PORTAL_OTP_DEV_ECHO"),
    )
