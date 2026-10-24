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
- ``COGNITO_EXTERNAL_ISSUER`` / ``COGNITO_CIS_PORTAL_CLIENT_ID``: the EXTERNAL pool and its ``cis-portal`` client,
  accepted on ``/crm/v1`` on Dev only, until AA-939 (ADR 0006b decision 6). Refused when ``ENVIRONMENT`` is not
  ``dev``/``test``/``local``.

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
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

SERVICE_NAME = "aa-crm-api"

SSL_MODES = frozenset({"require", "disable"})
INSECURE_ENVIRONMENTS = frozenset({"test", "local"})
# Environments where the EXTERNAL cis-portal bridge onto /crm/v1 is allowed (Dev only before AA-939).
PORTAL_BRIDGE_ENVIRONMENTS = frozenset({"dev", "test", "local"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

DEFAULT_RATE_LIMIT_PER_MINUTE = 120
DEFAULT_XFF_HOPS = 2
DEFAULT_RETENTION_UTC_HOUR = 19
DEFAULT_INTAKE_PER_MINUTE = 5
DEFAULT_INTAKE_FORM_PER_HOUR = 200
KEY_PROVIDERS = frozenset({"kms", "local"})
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
DEFAULT_DEK_CACHE_TTL_S = 300
MAX_DEK_CACHE_TTL_S = 900
_KMS_KEY_PREFIXES = ("alias/", "arn:aws:kms:")


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
    cognito_external_issuer: str = ""
    cognito_cis_portal_client_id: str = ""
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

    def __post_init__(self) -> None:
        if self.environment not in {"dev", "stg", "prod"} | INSECURE_ENVIRONMENTS:
            raise SettingsError(f"ENVIRONMENT must be dev, stg, prod, test or local, got {self.environment!r}")
        if self.db_ssl not in SSL_MODES:
            raise SettingsError(f"DB_SSL must be one of {sorted(SSL_MODES)}, got {self.db_ssl!r}")
        if self.db_ssl == "disable" and self.environment not in INSECURE_ENVIRONMENTS:
            raise SettingsError(
                f"DB_SSL=disable is only allowed when ENVIRONMENT is one of {sorted(INSECURE_ENVIRONMENTS)}"
            )
        for name, issuer in (
            ("COGNITO_WORKFORCE_ISSUER", self.cognito_workforce_issuer),
            ("COGNITO_PARTNER_ISSUER", self.cognito_partner_issuer),
            ("COGNITO_EXTERNAL_ISSUER", self.cognito_external_issuer),
        ):
            if issuer and (not issuer.startswith("https://") or issuer.endswith("/")):
                raise SettingsError(f"{name} must be an https:// URL without a trailing slash, got {issuer!r}")
        clients = {
            "COGNITO_CIS_ADMIN_CLIENT_ID": self.cognito_cis_admin_client_id,
            "COGNITO_BREAKGLASS_CLIENT_ID": self.cognito_breakglass_client_id,
            "COGNITO_CRM_STAFF_CLIENT_ID": self.cognito_crm_staff_client_id,
            "COGNITO_PARTNER_WEB_CLIENT_ID": self.cognito_partner_web_client_id,
            "COGNITO_CIS_PORTAL_CLIENT_ID": self.cognito_cis_portal_client_id,
        }
        for name, client_id in clients.items():
            if client_id and not client_id.isalnum():
                raise SettingsError(f"{name} must be a Cognito app client id (letters and digits), got {client_id!r}")
        workforce = [v for k, v in clients.items() if k in _WORKFORCE_CLIENTS and v]
        if len(workforce) != len(set(workforce)):
            # One id in two classes would let a token of one class pass the other's audience check.
            raise SettingsError("the WORKFORCE client ids (cis-admin, breakglass, crm-staff) must differ")
        if self.cognito_cis_portal_client_id and self.environment not in PORTAL_BRIDGE_ENVIRONMENTS:
            raise SettingsError("COGNITO_CIS_PORTAL_CLIENT_ID is a Dev-only bridge (ADR 0006b decision 6)")
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
        for origin in self.intake_page_origins:
            if not _ORIGIN.match(origin):
                raise SettingsError(f"INTAKE_PAGE_ORIGINS entries must be https origins, got {origin!r}")

    @property
    def intake_origin_check(self) -> bool:
        """Whether submissions must carry an allowed Origin (always outside test / local)."""
        return bool(self.intake_page_origins) or self.environment not in INSECURE_ENVIRONMENTS

    @property
    def keys_state(self) -> str:
        """``kms`` / ``local`` when field encryption can run, else ``not_configured`` (readiness body)."""
        if self.key_provider == "kms" and not self.kms_data_key_id:
            return "not_configured"
        return self.key_provider


_ORIGIN = re.compile(r"^https://[a-z0-9.-]+(:[0-9]{1,5})?$")


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
        cognito_external_issuer=_text(env, "COGNITO_EXTERNAL_ISSUER"),
        cognito_cis_portal_client_id=_text(env, "COGNITO_CIS_PORTAL_CLIENT_ID"),
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
    )
