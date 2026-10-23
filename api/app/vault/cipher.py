"""The field value format (design v3 §3.2): ``key_version ‖ nonce ‖ ciphertext ‖ tag``, AES-256-GCM.

- ``key_version``: 2 bytes, big endian (``crm.tenant_key.key_version`` 1–65535);
- ``nonce``: 12 random bytes per value (96-bit GCM nonce from ``os.urandom``);
- ``ciphertext ‖ tag``: what ``AESGCM.encrypt`` returns (16-byte tag at the end).

The AAD binds the value to where it lives: ``crm.v1 ‖ tenant_id ‖ table ‖ column ‖ row_id ‖ header`` (U+001F between
parts, each part validated so none can contain it). A value copied to another tenant, table, column or row, or with an
edited version header, fails the tag check. No home-made primitives: ``cryptography``'s AESGCM only.
"""

from __future__ import annotations

import os
import re
import struct
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HEADER = struct.Struct(">H")
NONCE_BYTES = 12
TAG_BYTES = 16
DEK_BYTES = 32
MIN_BLOB = HEADER.size + NONCE_BYTES + TAG_BYTES  # 30: an empty plaintext
MAX_VERSION = 65535

# A table name is also the audit ``object_kind`` of a denied decrypt (``app.audit._CODE``, CHECK
# ``audit_event_object_kind_chk``): keep both at 40 characters so a denial can always be recorded.
_TABLE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ROW_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
_SEP = "\x1f"


class FieldFormatError(ValueError):
    """The blob is not in the value format (too short)."""


def field_aad(tenant_id: uuid.UUID, table: str, column: str, row_id: uuid.UUID | str) -> bytes:
    """The AAD of one field value, without the version header (:func:`seal` / :func:`open_sealed` append it)."""
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    if not _TABLE.match(table) or not _NAME.match(column):
        raise ValueError("table (<= 40 chars) and column must be lower-case SQL names")
    row = str(row_id)
    if not _ROW_ID.match(row):
        raise ValueError("row_id must be an id (letters, digits, ':', '_', '-')")
    return (_SEP.join(("crm.v1", str(tenant_id), table, column, row)) + _SEP).encode("ascii")


def header_of(key_version: int) -> bytes:
    if not 1 <= key_version <= MAX_VERSION:
        raise ValueError("key_version must be 1-65535")
    return HEADER.pack(key_version)


def key_version_of(blob: bytes) -> int:
    if len(blob) < MIN_BLOB:
        raise FieldFormatError("value too short")
    return HEADER.unpack_from(blob)[0]


def seal(key: bytes, key_version: int, plaintext: bytes, aad_without_header: bytes) -> bytes:
    """Encrypt ``plaintext`` under ``key``; the AAD is ``aad_without_header`` (:func:`field_aad`) + the header."""
    header = header_of(key_version)
    nonce = os.urandom(NONCE_BYTES)
    return header + nonce + AESGCM(key).encrypt(nonce, plaintext, aad_without_header + header)


def open_sealed(key: bytes, blob: bytes, aad_without_header: bytes) -> bytes:
    """Decrypt a :func:`seal` value. Raises ``cryptography.exceptions.InvalidTag`` on any mismatch."""
    if len(blob) < MIN_BLOB:
        raise FieldFormatError("value too short")
    header = blob[: HEADER.size]
    nonce = blob[HEADER.size : HEADER.size + NONCE_BYTES]
    body = blob[HEADER.size + NONCE_BYTES :]
    return AESGCM(key).decrypt(nonce, body, aad_without_header + header)


__all__ = [
    "DEK_BYTES",
    "MIN_BLOB",
    "FieldFormatError",
    "InvalidTag",
    "field_aad",
    "header_of",
    "key_version_of",
    "open_sealed",
    "seal",
]
