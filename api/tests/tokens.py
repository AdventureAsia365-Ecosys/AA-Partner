"""Test-only Cognito pools: each a local RSA key and JWKS, minting Cognito-shaped access tokens.

``identity_settings()`` gives a :class:`~app.settings.Settings` with the three pools and all five clients configured;
``verifier()`` the production verifier built from it (``app.auth.identity_config``) with a fetcher that serves the
local key sets, so tokens go through the real byte-copied verifier. Pool ids contain TEST (the DM-6.10 scan allows
only fakes that say so).
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import identity_config
from app.auth.identity_verifier import MultiIssuerVerifier
from app.settings import Settings

WORKFORCE_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTWORKF"
PARTNER_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPARTN"
EXTERNAL_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTEXTER"
CIS_ADMIN_CLIENT = "cisadminclient000000000001"
BREAKGLASS_CLIENT = "breakglassclient0000000001"
CRM_STAFF_CLIENT = "crmstaffclient000000000001"
PARTNER_WEB_CLIENT = "partnerwebclient0000000001"
CIS_PORTAL_CLIENT = "cisportalclient00000000001"
OTHER_WORKFORCE_CLIENT = "bookingstaffclient00000001"
SUB = "3a1b0c9e-0000-4000-8000-00000000c0de"


def _jwk(private_key: Any, kid: str) -> dict[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


class LocalPool:
    def __init__(self, issuer: str, kid: str) -> None:
        self.issuer = issuer
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        return {"keys": [_jwk(self.private_key, self.kid)]}

    def token(
        self,
        client_id: str,
        groups: list[str] | None = None,
        *,
        sub: str = SUB,
        token_use: str = "access",  # noqa: S107 — a claim value, not a password
        exp_in: int = 900,
        **extra: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "iss": self.issuer,
            "client_id": client_id,
            "token_use": token_use,
            "iat": now,
            "exp": now + exp_in,
            "cognito:groups": groups or [],
            **extra,
        }
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": self.kid})


class LocalPools:
    """WORKFORCE, PARTNER and EXTERNAL pool stand-ins plus the settings and verifier that trust them.

    The EXTERNAL pool is NOT in the settings (AA-979 P2b removed the Dev ``cis-portal`` bridge onto ``/crm/v1``): it
    stays here only to mint the tokens every class must now refuse (:meth:`cis_portal`)."""

    def __init__(self) -> None:
        self.workforce = LocalPool(WORKFORCE_ISSUER, "w1")
        self.partner = LocalPool(PARTNER_ISSUER, "p1")
        self.external = LocalPool(EXTERNAL_ISSUER, "e1")

    def settings(self, **overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "environment": "test",
            "db_ssl": "disable",
            "cognito_workforce_issuer": WORKFORCE_ISSUER,
            "cognito_cis_admin_client_id": CIS_ADMIN_CLIENT,
            "cognito_breakglass_client_id": BREAKGLASS_CLIENT,
            "cognito_crm_staff_client_id": CRM_STAFF_CLIENT,
            "cognito_partner_issuer": PARTNER_ISSUER,
            "cognito_partner_web_client_id": PARTNER_WEB_CLIENT,
            **overrides,
        }
        return Settings(**values)

    async def fetch(self, url: str) -> dict[str, Any]:
        for pool in (self.workforce, self.partner, self.external):
            if url == f"{pool.issuer}/.well-known/jwks.json":
                return pool.jwks()
        raise AssertionError(f"unexpected JWKS URL {url}")

    def verifier(self, settings: Settings) -> MultiIssuerVerifier | None:
        return identity_config.build_verifier(identity_config.identity_from_settings(settings), self.fetch)

    # Convenience tokens, one per configured client.
    def cis_admin(self, groups: list[str] | None = None, **kw: Any) -> str:
        return self.workforce.token(CIS_ADMIN_CLIENT, ["cis-admin"] if groups is None else groups, **kw)

    def breakglass(self, **kw: Any) -> str:
        return self.workforce.token(BREAKGLASS_CLIENT, ["cis-admin"], **kw)

    def crm_staff(self, groups: list[str] | None = None, **kw: Any) -> str:
        return self.workforce.token(CRM_STAFF_CLIENT, ["crm-ops"] if groups is None else groups, **kw)

    def partner_web(self, **kw: Any) -> str:
        return self.partner.token(PARTNER_WEB_CLIENT, **kw)

    def cis_portal(self, **kw: Any) -> str:
        """A token of the retired EXTERNAL ``cis-portal`` client (validly signed by its pool, which no class trusts)."""
        return self.external.token(CIS_PORTAL_CLIENT, **kw)
