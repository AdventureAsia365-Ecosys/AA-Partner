"""Blind indexes (design v3 §4.2, lane §5.2): exact search and dedupe over encrypted fields.

- **Key** per tenant and column: ``K_idx = HKDF-SHA256(DEK_vault_root, salt = tenant_id bytes, info = "bidx:" ‖
  table.column)``. Equal values in two tenants give different indexes; two columns never share a key.
- **Value**: ``HMAC-SHA256(K_idx, normalise(value))`` truncated (CipherSweet style): 16 bytes for email and phone, 8 for
  the full normalised name, 4 per name token (deliberately lossy: a token match is a candidate, confirmed after
  decrypting).
- **Normalisation**: email trim + lowercase; phone E.164 (libphonenumber, ``phonenumberslite``); name NFKD, combining
  marks removed, ``đ``/``Đ`` -> ``d``, casefolded, tokens split on anything that is not a letter or a digit.

Fuzzy or partial-name search is not offered (it needs a security review first, design R35).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
import uuid

import phonenumbers
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

EMAIL_BYTES = 16
PHONE_BYTES = 16
NAME_BYTES = 8
TOKEN_BYTES = 4
MAX_TOKENS = 16

_COLUMN = re.compile(r"^[a-z][a-z0-9_]{0,62}\.[a-z][a-z0-9_]{0,62}$")
_SPLIT = re.compile(r"[^\w]+|_+")
# Letters with no Unicode decomposition that Vietnamese (and a few other) names use.
_FOLD = str.maketrans({"đ": "d", "Đ": "d", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "ß": "ss"})


class NormalisationError(ValueError):
    """The value cannot be normalised (not an email, not a phone number, an empty name). Code only, no value."""


def index_key(root_dek: bytes, tenant_id: uuid.UUID, column: str) -> bytes:
    """The HMAC key of one ``table.column`` index in one tenant."""
    if not _COLUMN.match(column):
        raise ValueError("column must be 'table.column'")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=tenant_id.bytes, info=b"bidx:" + column.encode("ascii"))
    return hkdf.derive(root_dek)


def mac(key: bytes, normalised: str, size: int) -> bytes:
    return hmac.new(key, normalised.encode("utf-8"), hashlib.sha256).digest()[:size]


def normalise_email(value: str) -> str:
    email = value.strip().lower()
    local, at, domain = email.partition("@")
    if not at or not local or not domain or "@" in domain or any(c.isspace() for c in email):
        raise NormalisationError("invalid_email")
    return email


def normalise_phone(value: str, region: str | None = None) -> str:
    """E.164 (``+84912345678``). Without ``region`` the number must start with ``+``."""
    try:
        number = phonenumbers.parse(value, region.upper() if region else None)
    except phonenumbers.NumberParseException:
        raise NormalisationError("invalid_phone") from None
    if not phonenumbers.is_valid_number(number):
        raise NormalisationError("invalid_phone")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def name_tokens(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", value.translate(_FOLD))
    stripped = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    tokens = [t for t in _SPLIT.split(stripped) if t]
    if not tokens:
        raise NormalisationError("invalid_name")
    return tokens


def normalise_name(value: str) -> str:
    return " ".join(name_tokens(value))


def email_index(key: bytes, value: str) -> bytes:
    return mac(key, normalise_email(value), EMAIL_BYTES)


def phone_index(key: bytes, value: str, region: str | None = None) -> bytes:
    return mac(key, normalise_phone(value, region), PHONE_BYTES)


def name_index(key: bytes, value: str) -> bytes:
    return mac(key, normalise_name(value), NAME_BYTES)


def name_token_indexes(key: bytes, value: str) -> list[bytes]:
    """One 4-byte index per distinct token, in first-seen order, at most :data:`MAX_TOKENS`."""
    out: list[bytes] = []
    for token in name_tokens(value):
        digest = mac(key, token, TOKEN_BYTES)
        if digest not in out:
            out.append(digest)
    return out[:MAX_TOKENS]
