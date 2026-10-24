"""Settings from the environment: defaults, fail-closed identity, unsafe values refused."""

from __future__ import annotations

import pytest

from app.settings import Settings, SettingsError, load_settings

POOL = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTWORKF"


def test_defaults_without_environment() -> None:
    s = load_settings({})
    assert s.environment == "dev"
    assert s.db_ssl == "require"
    assert s.database_url is None
    assert s.worker_enabled is False  # no DSN
    assert s.job_retention_dry_run is True
    # Identity has no default at all (public repo, DM-6.10): every class accepts nothing until Terraform sets it.
    assert (s.cognito_workforce_issuer, s.cognito_partner_issuer, s.cognito_external_issuer) == ("", "", "")
    assert s.rate_limit_public_per_minute == 120
    assert s.rate_limit_xff_hops == 2


def test_worker_defaults_on_in_dev_with_a_dsn() -> None:
    assert load_settings({"DATABASE_URL": "postgresql://crm_app@h/db"}).worker_enabled is True
    assert load_settings({"DATABASE_URL": "postgresql://crm_app@h/db", "ENVIRONMENT": "test"}).worker_enabled is False
    assert load_settings({"DATABASE_URL": "x", "WORKER_ENABLED": "false"}).worker_enabled is False


def test_dsn_is_not_in_repr() -> None:
    assert "secret" not in repr(load_settings({"DATABASE_URL": "postgresql://crm_app:secret@h/db"}))


def test_identity_values_are_read() -> None:
    s = load_settings(
        {
            "COGNITO_WORKFORCE_ISSUER": POOL,
            "COGNITO_CIS_ADMIN_CLIENT_ID": "abc123",
            "COGNITO_BREAKGLASS_CLIENT_ID": "def456",
            "JOB_RETENTION_DRY_RUN": "off",
            "RATE_LIMIT_PUBLIC_PER_MINUTE": "0",
            "JOB_RETENTION_UTC_HOUR": "3",
        }
    )
    assert (s.cognito_workforce_issuer, s.cognito_cis_admin_client_id, s.cognito_breakglass_client_id) == (
        POOL,
        "abc123",
        "def456",
    )
    assert s.job_retention_dry_run is False
    assert s.job_retention_utc_hour == 3
    assert load_settings({}).job_retention_utc_hour == 19
    assert s.rate_limit_public_per_minute == 0


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"ENVIRONMENT": "staging"}, "ENVIRONMENT"),
        ({"DB_SSL": "disable"}, "DB_SSL=disable"),
        ({"DB_SSL": "prefer"}, "DB_SSL"),
        ({"COGNITO_WORKFORCE_ISSUER": "http://cognito.test/pool"}, "https"),
        ({"COGNITO_PARTNER_ISSUER": POOL + "/"}, "trailing slash"),
        ({"COGNITO_CIS_ADMIN_CLIENT_ID": "not a client"}, "client id"),
        ({"COGNITO_CIS_ADMIN_CLIENT_ID": "same1", "COGNITO_BREAKGLASS_CLIENT_ID": "same1"}, "must differ"),
        ({"ENVIRONMENT": "prod", "COGNITO_CIS_PORTAL_CLIENT_ID": "abc"}, "Dev-only"),
        ({"WORKER_ENABLED": "maybe"}, "WORKER_ENABLED"),
        ({"RATE_LIMIT_PUBLIC_PER_MINUTE": "many"}, "integer"),
        ({"RATE_LIMIT_PUBLIC_PER_MINUTE": "-1"}, ">= 0"),
        ({"RATE_LIMIT_XFF_HOPS": "0"}, "between 1 and 5"),
        ({"JOB_RETENTION_UTC_HOUR": "24"}, "between 0 and 23"),
        ({"JOB_RETENTION_UTC_HOUR": "-1"}, "between 0 and 23"),
        ({"SENTRY_DSN": "http://x"}, "SENTRY_DSN"),
        ({"SENTRY_REQUIRED": "true"}, "SENTRY_REQUIRED"),
        ({"KEY_PROVIDER": "vault9"}, "KEY_PROVIDER must be"),
        ({"KEY_PROVIDER": "local"}, "KEY_PROVIDER=local"),
        ({"ENVIRONMENT": "prod", "KEY_PROVIDER": "local"}, "KEY_PROVIDER=local"),
        ({"LOCAL_KEK_B64": "abc"}, "LOCAL_KEK_B64"),
        ({"KMS_DATA_KEY_ID": "crm-dev/data"}, "KMS_DATA_KEY_ID"),
        ({"DEK_CACHE_TTL_SECONDS": "901"}, "DEK_CACHE_TTL_SECONDS"),
        ({"DEK_CACHE_TTL_SECONDS": "-1"}, "DEK_CACHE_TTL_SECONDS"),
        ({"SCRUB_GUARDRAIL_ID": "abc123"}, "set together"),
        ({"SCRUB_GUARDRAIL_VERSION": "1"}, "set together"),
        ({"SCRUB_GUARDRAIL_ID": "ABC-1", "SCRUB_GUARDRAIL_VERSION": "1"}, "guardrail id"),
        ({"SCRUB_GUARDRAIL_ID": "abc123", "SCRUB_GUARDRAIL_VERSION": "v1"}, "published version"),
        ({"SCRUB_GUARDRAIL_REGION": "mars"}, "AWS region"),
        ({"SCRUB_DETECTOR_TIMEOUT_MS": "100"}, "SCRUB_DETECTOR_TIMEOUT_MS"),
        ({"SCRUB_DETECTOR_TIMEOUT_MS": "60000"}, "SCRUB_DETECTOR_TIMEOUT_MS"),
    ],
)
def test_unsafe_values_are_refused(env: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        load_settings(env)


def test_insecure_db_only_in_test_or_local() -> None:
    assert Settings(environment="local", db_ssl="disable").db_ssl == "disable"


def test_key_provider_defaults_per_environment() -> None:
    dev = load_settings({})
    assert (dev.key_provider, dev.kms_data_key_id, dev.keys_state, dev.dek_cache_ttl_seconds) == (
        "kms",
        "",
        "not_configured",
        300,
    )
    assert load_settings({"KMS_DATA_KEY_ID": "alias/crm-dev/data"}).keys_state == "kms"
    for key_id in (
        "arn:aws:kms:us-west-1:123456789012:key/0f0e0d0c-0b0a-4908-8706-050403020100",
        "0f0e0d0c-0b0a-4908-8706-050403020100",
    ):
        assert load_settings({"KMS_DATA_KEY_ID": key_id}).kms_data_key_id == key_id
    test = load_settings({"ENVIRONMENT": "test", "LOCAL_KEK_B64": "A" * 44, "DEK_CACHE_TTL_SECONDS": "0"})
    assert (test.key_provider, test.keys_state, test.dek_cache_ttl_seconds) == ("local", "local", 0)
    assert "AAAA" not in repr(test)


PARTNER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPARTN"


def test_team_values_are_read_and_checked() -> None:
    s = load_settings(
        {
            "COGNITO_PARTNER_ISSUER": PARTNER,
            "COGNITO_PARTNER_POOL_ID": "us-west-1_TESTPARTN",
            "PARTNER_WEB_ORIGIN": "https://Partner.Example.TEST/",
            "SES_FROM_ADDRESS": "No-Reply@Example.test",
            "SES_CONFIGURATION_SET": "aa-email-dev-transactional",
            "INVITATION_TTL_HOURS": "72",
            "RATE_LIMIT_CLAIM_PER_MINUTE": "4",
        }
    )
    assert (s.cognito_partner_pool_id, s.partner_web_origin, s.ses_from_address) == (
        "us-west-1_TESTPARTN",
        "https://partner.example.test",
        "no-reply@example.test",
    )
    assert (s.invitation_ttl_hours, s.rate_limit_claim_per_minute, s.invitation_origin) == (
        72,
        4,
        "https://partner.example.test",
    )
    assert load_settings({}).invitation_origin == ""
    assert load_settings({"ENVIRONMENT": "test"}).invitation_origin == "https://partner.local.test"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"COGNITO_PARTNER_ISSUER": PARTNER, "COGNITO_PARTNER_POOL_ID": "not a pool"}, "COGNITO_PARTNER_POOL_ID"),
        ({"COGNITO_PARTNER_ISSUER": PARTNER, "COGNITO_PARTNER_POOL_ID": "us-west-1_OTHER"}, "pool of"),
        ({"PARTNER_WEB_ORIGIN": "http://partner.example.test"}, "PARTNER_WEB_ORIGIN"),
        ({"SES_FROM_ADDRESS": "nobody", "SES_CONFIGURATION_SET": "x"}, "SES_FROM_ADDRESS"),
        ({"SES_FROM_ADDRESS": "a@example.test"}, "set together"),
        ({"INVITATION_TTL_HOURS": "0"}, "INVITATION_TTL_HOURS"),
        ({"INVITATION_TTL_HOURS": "721"}, "INVITATION_TTL_HOURS"),
        ({"RATE_LIMIT_CLAIM_PER_MINUTE": "-1"}, "RATE_LIMIT_CLAIM_PER_MINUTE"),
    ],
)
def test_unsafe_team_values_are_refused(env: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        load_settings(env)


def test_scrub_guardrail_values_are_read() -> None:
    """AA-938: the F3 guardrail of pass 3 (AA-CIS-Infra crm_scrub.tf); without it every scrub fails closed."""
    default = load_settings({})
    assert (default.scrub_guardrail_id, default.scrub_guardrail_region, default.scrub_detector_timeout_ms) == (
        "",
        "us-west-1",
        3000,
    )
    got = load_settings(
        {"SCRUB_GUARDRAIL_ID": "abc123", "SCRUB_GUARDRAIL_VERSION": "DRAFT", "SCRUB_DETECTOR_TIMEOUT_MS": "1500"}
    )
    assert (got.scrub_guardrail_id, got.scrub_guardrail_version, got.scrub_detector_timeout_ms) == (
        "abc123",
        "DRAFT",
        1500,
    )
