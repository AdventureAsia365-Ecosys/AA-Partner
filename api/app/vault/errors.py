"""Vault errors. Messages carry codes only: never a value, a key, a KMS response or the wrapped DEK."""

from __future__ import annotations

from app.errors import Forbidden, NotFound, Unavailable


class DecryptionDenied(Forbidden):
    """A value could not be opened in this tenant's context (tampered, another tenant's, another row's, a wrong key).

    The vault records an ``audit_event`` with outcome ``denied`` before raising; ``tenant_tx()`` keeps that row even
    when this exception ends the request.
    """

    def __init__(self, reason: str) -> None:
        super().__init__("decrypt_denied", reason=reason)
        self.reason = reason


class KeyDestroyed(NotFound):
    """The tenant's key was destroyed (offboarding crypto-shred): the data is gone for good."""

    def __init__(self) -> None:
        super().__init__("key_destroyed")


class KeyUnavailable(Unavailable):
    """KMS (or the local keyring) could not be reached or refused for a reason that is not about this value."""

    def __init__(self, reason: str = "key_unavailable") -> None:
        super().__init__("key_unavailable", reason=reason)
        self.reason = reason


class KeyProviderNotConfigured(Unavailable):
    """``KEY_PROVIDER=kms`` without ``KMS_DATA_KEY_ID`` (the task definition lacks it)."""

    def __init__(self) -> None:
        super().__init__("key_provider_not_configured")


class UnwrapDenied(Exception):
    """Internal: a wrapper refused to unwrap this DEK under this context (wrong tenant / purpose, tampered blob).

    Turned into :class:`DecryptionDenied` (audited) by the provider's callers.
    """
