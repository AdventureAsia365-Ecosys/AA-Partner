"""Field encryption for the partner CRM (AA-934, design v3 card CRM-06, §4.1–4.2; lane §5.1–5.2).

The pieces, from the bottom up:

- :mod:`app.vault.cipher`: the AES-256-GCM value format ``key_version ‖ nonce ‖ ciphertext ‖ tag`` with the AAD
  ``tenant ‖ table ‖ column ‖ row id`` (a value cannot be moved to another tenant, table, column or row).
- :mod:`app.vault.blind_index`: normalisation and truncated-HMAC blind indexes from an HKDF of the tenant's vault DEK
  (exact search and dedupe only).
- :mod:`app.vault.wrapping`: the KEK wrappers. :class:`~app.vault.wrapping.KmsKeyWrapper` (KMS
  ``alias/crm-<env>/data``, encryption context ``{tenant_id, purpose}``) and
  :class:`~app.vault.wrapping.LocalKeyWrapper` (tests and local runs only).
- :mod:`app.vault.store`: the DEK store, ``crm.tenant_key`` on Dev (pluggable: S3 at Go-real, design §3.1).
- :mod:`app.vault.provider`: :class:`~app.vault.provider.KeyProvider` — ``get_dek`` / ``rotate`` / ``rewrap`` /
  ``destroy`` with the per-process DEK cache (about 5 minutes) and the per-transaction memo.
- :mod:`app.vault.vault`: :class:`~app.vault.vault.Vault`, what request code uses inside ``tenant_tx()``: encrypt,
  decrypt (a refusal is audited ``denied``), blind indexes, and the integration-secret path.

One DEK per tenant per purpose (``vault``, ``docs``, ``integration``, ``scrub``, ``ops_release``, ``export``,
``mail``); no per-traveller DEK (design S1).
"""

from app.vault.errors import DecryptionDenied, KeyDestroyed, KeyProviderNotConfigured, KeyUnavailable
from app.vault.provider import PURPOSES, KeyProvider, build_key_provider
from app.vault.vault import Vault

__all__ = [
    "PURPOSES",
    "DecryptionDenied",
    "KeyDestroyed",
    "KeyProvider",
    "KeyProviderNotConfigured",
    "KeyUnavailable",
    "Vault",
    "build_key_provider",
]
