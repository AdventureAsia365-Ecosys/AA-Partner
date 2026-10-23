"""Issuers and route classes of the AA-Partner API (root ADR 0006b decision 6, ADR 0006 §8; AA-933 / CRM-05).

Three route classes, each accepting only its own ``(issuer, client)`` pairs:

=================  =========================================================  ===============================
Route class        Accepts                                                    Authorisation after the guard
=================  =========================================================  ===============================
``/crm/v1``        PARTNER x {AA-Partner web}; on Dev before AA-939 also      CRM membership + role from
                   EXTERNAL x {cis-portal}                                     ``crm.member`` (``tenant_tx()``)
``/crm/ops/v1``    WORKFORCE x {cis-admin} (the CIS admin BFF forwards it)    group ``cis-admin`` reads,
                                                                              ``crm-platform-admin`` writes
``/crm/staff/v1``  WORKFORCE x {crm-staff}                                    group ``crm-ops`` or ``crm-support``
=================  =========================================================  ===============================

``breakglass`` (WORKFORCE) is accepted by no class. It is still known to the verifier, so a breakglass token is
**verified** and then refused with 403 like a ``cis-admin`` token on ``/crm/v1`` (card R test), never mistaken for a
malformed token.

One :class:`~app.auth.identity_verifier.MultiIssuerVerifier` (byte copy of AA-CIS-App ``api/core/identity_verifier.py``)
verifies every token against the union of the configured pairs; the class check is :mod:`app.auth.route_guard`. The
values come only from the environment (``app.settings``); a pair with an empty issuer or client id is left out, so an
unconfigured class accepts nothing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.auth.identity_verifier import IssuerConfig, JwksFetcher, MultiIssuerVerifier

if TYPE_CHECKING:
    from app.settings import Settings

log = logging.getLogger(__name__)

# Issuer kinds (IssuerConfig.name).
WORKFORCE = "workforce"
PARTNER = "partner"
EXTERNAL = "external"

# Client kinds.
CIS_ADMIN = "cis-admin"
BREAKGLASS = "breakglass"
CRM_STAFF = "crm-staff"
PARTNER_WEB = "partner-web"
CIS_PORTAL = "cis-portal"

# WORKFORCE groups (ADR 0006b decision 6).
GROUP_CIS_ADMIN = "cis-admin"
GROUP_CRM_PLATFORM_ADMIN = "crm-platform-admin"
GROUP_CRM_OPS = "crm-ops"
GROUP_CRM_SUPPORT = "crm-support"


@dataclass(frozen=True)
class RouteClass:
    """One route class: its path prefix, the (issuer kind, client kind) pairs it accepts and the groups it needs."""

    name: str
    prefix: str
    accepts: frozenset[tuple[str, str]]
    # Empty = no group needed (the agency class authorises by CRM membership instead).
    groups_any: frozenset[str] = frozenset()


AGENCY = RouteClass(
    name="agency",
    prefix="/crm/v1",
    accepts=frozenset({(PARTNER, PARTNER_WEB), (EXTERNAL, CIS_PORTAL)}),
)
OPS = RouteClass(
    name="ops",
    prefix="/crm/ops/v1",
    accepts=frozenset({(WORKFORCE, CIS_ADMIN)}),
    groups_any=frozenset({GROUP_CIS_ADMIN, GROUP_CRM_PLATFORM_ADMIN}),
)
STAFF = RouteClass(
    name="staff",
    prefix="/crm/staff/v1",
    accepts=frozenset({(WORKFORCE, CRM_STAFF)}),
    groups_any=frozenset({GROUP_CRM_OPS, GROUP_CRM_SUPPORT}),
)
ROUTE_CLASSES: tuple[RouteClass, ...] = (AGENCY, OPS, STAFF)


@dataclass(frozen=True)
class Identity:
    """The configured issuers: issuer kind -> issuer URL, and (issuer kind, client id) -> client kind."""

    issuers: dict[str, str]
    clients: dict[tuple[str, str], str]

    def issuer_kind(self, iss: str) -> str | None:
        for kind, url in self.issuers.items():
            if url == iss:
                return kind
        return None

    def client_kind(self, issuer_kind: str, client_id: str) -> str | None:
        return self.clients.get((issuer_kind, client_id))

    def issuer_configs(self) -> tuple[IssuerConfig, ...]:
        configs = []
        for kind, url in self.issuers.items():
            ids = frozenset(cid for (ik, cid) in self.clients if ik == kind)
            if ids:
                configs.append(IssuerConfig(issuer=url, client_ids=ids, name=kind))
        return tuple(configs)


def identity_from_settings(settings: Settings) -> Identity:
    """The configured pairs. A pair with an empty issuer or client id is left out (that class accepts nothing)."""
    pairs = [
        (WORKFORCE, settings.cognito_workforce_issuer, CIS_ADMIN, settings.cognito_cis_admin_client_id),
        (WORKFORCE, settings.cognito_workforce_issuer, BREAKGLASS, settings.cognito_breakglass_client_id),
        (WORKFORCE, settings.cognito_workforce_issuer, CRM_STAFF, settings.cognito_crm_staff_client_id),
        (PARTNER, settings.cognito_partner_issuer, PARTNER_WEB, settings.cognito_partner_web_client_id),
        (EXTERNAL, settings.cognito_external_issuer, CIS_PORTAL, settings.cognito_cis_portal_client_id),
    ]
    issuers: dict[str, str] = {}
    clients: dict[tuple[str, str], str] = {}
    for issuer_kind, issuer, client_kind, client_id in pairs:
        if not issuer or not client_id:
            continue
        issuers[issuer_kind] = issuer
        clients[(issuer_kind, client_id)] = client_kind
    urls = list(issuers.values())
    if len(urls) != len(set(urls)):
        raise ValueError("two issuer kinds share one issuer URL: the route classes could not tell them apart")
    return Identity(issuers=issuers, clients=clients)


def build_verifier(identity: Identity, fetcher: JwksFetcher | None = None) -> MultiIssuerVerifier | None:
    """The verifier over every configured pair, or None when nothing is configured (every guarded route then 401)."""
    configs = identity.issuer_configs()
    if not configs:
        return None
    return MultiIssuerVerifier(configs, fetcher)


async def prefetch_jwks(verifier: MultiIssuerVerifier) -> dict[str, int]:
    """Load every issuer's key set once. Never raises: a failure is logged and the verifier retries on first use."""
    try:
        counts = await verifier.prefetch()
    except Exception as exc:  # noqa: BLE001 — start-up must never fail on the identity prefetch
        log.warning("identity_jwks_prefetch_failed error=%s", type(exc).__name__)
        return {}
    for iss, count in counts.items():
        if count:
            log.info("identity_jwks_prefetched issuer=%s keys=%s", iss, count)
        else:
            log.warning("identity_jwks_prefetch_empty issuer=%s", iss)
    return counts


def start_jwks_prefetch(
    verifier: MultiIssuerVerifier | None, settings: Settings
) -> asyncio.Task[dict[str, int]] | None:
    """Lifespan hook: :func:`prefetch_jwks` in the background (never delays start-up). None in tests or when no issuer
    is configured."""
    if verifier is None or settings.environment == "test":
        return None
    return asyncio.create_task(prefetch_jwks(verifier), name="crm-identity-jwks-prefetch")


async def stop_jwks_prefetch(task: asyncio.Task[dict[str, int]] | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
