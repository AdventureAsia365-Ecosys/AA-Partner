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

Rate limits (ADR 0010 decision 2: CloudFront → VPC origin bypasses API Gateway throttling):

- ``RATE_LIMIT_PUBLIC_PER_MINUTE``: requests per client address per minute on the unauthenticated routes (default
  120; 0 turns the limit off).
- ``RATE_LIMIT_XFF_HOPS``: which ``X-Forwarded-For`` entry is the client, counted from the right (default 2: the
  viewer address CloudFront appended, then the address the ALB appended). Verified live after the first deploy.
"""

from __future__ import annotations

import os
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
        if self.sentry_dsn and not self.sentry_dsn.startswith("https://"):
            raise SettingsError("SENTRY_DSN must be an https:// URL")  # the value is never echoed
        if self.sentry_required and not self.sentry_dsn:
            raise SettingsError("SENTRY_REQUIRED is on but SENTRY_DSN is empty")


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
    )
